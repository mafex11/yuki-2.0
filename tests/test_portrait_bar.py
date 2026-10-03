"""The portrait's admission bar (report 50, appA2): the bar in the prompts, the bar gate on a run's new versions,
and the bar pass that retires facts failing it (pinned facts kept and reported, the pass reversible).

Stub models, temp databases, invented people and numbers. One opt-in live check (``YUKI_LIVE_BAR=1``) sends the
invented fixtures to the real judge.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.store import FactChange, Store

T0 = datetime(2026, 8, 14, 21, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0)


class ToolClient:
    """``messages.stream`` answering each tool with a queued input (the last one repeats); a tool with no answer
    raises, like a failed call."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def _response(self, request):
        self.requests.append(request)
        name = request["tools"][0]["name"]
        data = self.answers[name].pop(0) if len(self.answers[name]) > 1 else self.answers[name][0]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use", usage=usage())

    def create(self, **request):
        return self._response(request)

    def stream(self, **request):
        response = self._response(request)

        class _S:
            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False

            def get_final_message(s):
                return response

        return _S()

    def tools_called(self):
        return [r["tools"][0]["name"] for r in self.requests]


RENDER = {"save_portrait": [{"text": "Work", "profile": "", "personality": ""}]}

#: Invented fixtures: what fails the bar and what passes.
QUOTA = ("The user's harness rotates across four Claude accounts on Mac and PC. On the evening of 2026-08-14, with "
         "15 parallel agents running, the accounts' combined quota fell to 160% of 400% by 21:40, burning at about "
         "2.9%/minute; the coding tool projected all four would hit 100% around 22:30. Cumulative usage from "
         "2026-08-08 to 2026-08-14 reached $4,210 across 280 sessions.")
ADVICE = "The coding agent advised the user to run fewer parallel chats until 23:50 to stay within limits."
MIX = "From 2026-08-08 to 2026-08-14 the user's tokens were 81% one model and 19% another, 12.4B in total."
SISTER = "Mira, the user's sister, talks with them on WhatsApp most evenings about her exams."
WORK = "The user builds Pinecone, a desktop assistant, by directing coding agents and reviewing their work."
HABIT = "On 4 of the last 5 weekday evenings the user practised the cello from about 19:30 to 20:30."
LASTING = "The user runs several Claude accounts for coding across their Mac and PC."


def fail(ref, category, lasting=None, reason="a tool's readings"):
    return {"id": ref, "category": category, "lasting": lasting,
            "shown": "You run several Claude accounts for coding on your Mac and PC." if lasting else None,
            "summary": "Several Claude accounts for coding" if lasting else None, "reason": reason}


def worker_with(store, answers):
    from yuki.memory.portrait import PortraitWorker

    client = ToolClient(answers)
    return PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp())), client


# -- (1) the bar in the prompts ------------------------------------------------------------------------


def test_the_admission_bar_is_in_every_prompt_that_writes_or_keeps_facts():
    from yuki.memory.dreaming import dream_system_prompt
    from yuki.memory.portrait import ADMISSION_BAR, BAR_SYSTEM_PROMPT, OPS_SYSTEM_PROMPT

    for prompt in (OPS_SYSTEM_PROMPT, BAR_SYSTEM_PROMPT, dream_system_prompt()):
        assert ADMISSION_BAR in prompt
    # durable, about the person, useful; the worked negative of the quota evening and its one lasting line
    for part in ("(a) durable", "(b) about the person", "(c) useful", "quota fell to", "several Claude accounts",
                 "FAILS", "PASSES"):
        assert part in ADMISSION_BAR
    assert ADMISSION_BAR.count("- FAILS:") >= 4 and ADMISSION_BAR.count("- PASSES:") >= 3
    assert "{" not in dream_system_prompt()


def test_bar_verdicts_are_checked_against_the_listed_facts():
    from yuki.memory.portrait import parse_bar_verdicts

    got, bad = parse_bar_verdicts({"failing": [
        fail("F1", "usage_metrics", LASTING), fail("F2", "transient_state", LASTING),   # the same line twice
        fail("F9", "usage_metrics"), fail("F3", "made_up"), fail("F1", "usage_metrics"),
    ]}, ["F1", "F2", "F3"])
    assert [(v.ref, v.lasting) for v in got] == [("F1", LASTING), ("F2", None)]
    assert len(bad) == 3


# -- (2) the bar gate on a run's new versions -----------------------------------------------------------


def _ops(j):
    def add(text, kind="work", subject="Claude"):
        return {"op": "ADD", "fact_id": None, "kind": kind, "subject": subject, "text": text, "summary": None,
                "shown": None, "confidence": 0.7, "importance": 5, "journal_ids": [j], "episode_ids": [],
                "based_on_facts": [], "reason": "x", "loop": None, "aspect": None}

    return [add(QUOTA), add(SISTER, "person", "Mira"), add(ADVICE)]


def test_the_bar_gate_refuses_telemetry_and_keeps_the_one_lasting_line(store):
    j = store.add_journal(T0, None, "Coding tool", "The usage panel showed the accounts' quota.", 5)
    worker, client = worker_with(store, {"judge_bar": [{"failing": [
        fail("N1", "usage_metrics", LASTING), fail("N3", "ai_tool_output")]}]})
    journal = store.journal_between()
    changes, outcomes = worker._validate(_ops(j), [], journal, T0 + 3600)
    from yuki.memory.store import PortraitRunStats

    stats = PortraitRunStats(kind="nightly", model=worker.model)
    kept = worker._bar_gate(None, changes, outcomes, journal, stats)
    assert [c.text for c in kept] == [LASTING, SISTER]
    assert kept[0].shown and kept[0].summary and kept[0].source_ids == [j]
    assert outcomes[2].status == "rejected: fails the admission bar (ai_tool_output)"
    assert outcomes[0].status == outcomes[1].status == "applied"
    # one forced call, the facts fenced, no thinking
    req = client.requests[-1]
    assert req["tool_choice"] == {"type": "tool", "name": "judge_bar"} and "thinking" not in req
    assert "N1 [work]" in req["messages"][0]["content"] and "UNTRUSTED_DATA" in req["messages"][0]["content"]


def test_the_bar_gate_lets_corrections_and_loops_through_and_fails_open(store):
    j = store.add_journal(T0, None, "Slack", "Mira asked the user for the notes by Friday.", 6)
    worker, client = worker_with(store, {})            # no judge answer: the call fails
    journal = store.journal_between()
    changes, outcomes = worker._validate(_ops(j), [], journal, T0 + 3600)
    from yuki.memory.store import PortraitRunStats

    kept = worker._bar_gate(None, changes, outcomes, journal, PortraitRunStats(kind="nightly", model=worker.model))
    assert kept == changes and all(o.status == "applied" for o in outcomes)     # failed open, logged
    # nothing of the model's own to judge: no call at all
    loop = FactChange("ADD", None, "open_loop", "Mira", "The user owes Mira the notes.", 0.8, [j], T0)
    from yuki.memory.portrait import OpOutcome

    out = OpOutcome("ADD", None, "open_loop", "Mira", loop.text, 0.8, [j], [], "x")
    before = len(client.requests)
    assert worker._bar_gate(None, [loop], [out], journal, PortraitRunStats(kind="nightly", model=worker.model)) == [loop]
    assert len(client.requests) == before


def test_a_nightly_run_goes_through_the_bar_gate(store):
    j = store.add_journal(T0, None, "Coding tool", "The usage panel showed the accounts' quota.", 5)
    worker, client = worker_with(store, {
        "update_portrait": [{"operations": _ops(j), "questions": []}],
        "judge_bar": [{"failing": [fail("N1", "usage_metrics"), fail("N3", "ai_tool_output")]}], **RENDER})
    result = worker.run("nightly", now=T0 + 3600)
    assert result.ok, result.error
    assert client.tools_called()[:2] == ["update_portrait", "judge_bar"]
    texts = [f.text for f in store.portrait_facts()]
    assert texts == [SISTER]
    assert result.stats.ops_rejected == 2 and result.stats.ops_add == 1


# -- (3) the bar pass ------------------------------------------------------------------------------------


def _facts(store):
    j = [store.add_journal(T0 + i, None, "Coding tool", f"reading {i}", 5) for i in range(4)]
    written = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "Claude accounts", QUOTA, 0.8, [j[0], j[1]], T0, importance=6),
        FactChange("ADD", None, "work", "token mix", MIX, 0.6, [j[2]], T0),
        FactChange("ADD", None, "person", "Mira", SISTER, 0.9, [j[3]], T0),
        FactChange("ADD", None, "work", "Pinecone", WORK, 0.9, [j[3]], T0),
        FactChange("ADD", None, "behaviour", "cello", HABIT, 0.6, [j[3]], T0),
        FactChange("ADD", None, "work", "advice", ADVICE, 0.5, [j[1]], T0),
        FactChange("ADD", None, "open_loop", "Mira", "The user owes Mira the notes.", 0.8, [j[3]], T0),
    ])
    ids = [new for _ok, new in written]
    store.set_fact_pinned(ids[5], True)       # the user pinned the advice: never retired, reported
    store.mark_fact_reviewed(ids[0], T0 + 10)  # answered in "To review": its lasting line does not ask again
    return j, ids


def _verdicts(ids):
    quota, mix, _sister, _work, _habit, advice, _loop = ids
    return [{"failing": [fail(f"F{quota}", "usage_metrics", LASTING), fail(f"F{mix}", "usage_metrics"),
                         fail(f"F{advice}", "ai_tool_output")]}]


def test_the_bar_pass_retires_telemetry_keeps_durable_facts_and_pinned_ones(store):
    from yuki.memory.dreaming import BarPass

    j, ids = _facts(store)
    quota, mix, sister, work, habit, advice, loop = ids
    worker, client = worker_with(store, {"judge_bar": _verdicts(ids), **RENDER})
    result = BarPass(worker).run(now=T0 + 7200)
    assert result.ok, result.error
    assert result.retired == 2 and result.lasting == 1
    assert result.categories == {"usage_metrics": 2}
    assert result.pinned == [{"id": advice, "kind": "work", "category": "ai_tool_output"}]
    current = {f.id: f for f in store.portrait_facts()}
    assert {sister, work, habit, advice, loop} <= set(current) and quota not in current and mix not in current
    # bi-temporal: the retired versions are kept as history, ended, not superseded
    old = store.portrait_fact(quota)
    assert old.valid_to is not None and old.superseded_by is None and old.text == QUOTA
    [new] = [f for f in current.values() if f.text == LASTING]
    assert new.kind == "work" and sorted(new.source_ids) == sorted([j[0], j[1]]) and new.importance == 6
    assert new.shown and new.summary and new.reviewed_at == T0 + 10 and not new.pinned
    # open loops and corrections are never shown to the pass
    content = client.requests[0]["messages"][0]["content"]
    assert f"F{loop} " not in content and f"F{quota} [work]" in content and f"F{advice} [work] " in content
    assert "pinned" in content
    # diary lines, pass bar, and the run recorded
    diary = store.changelog(pass_="bar")
    assert len(diary) == 2 and all("admission bar" in d["text"] for d in diary)
    assert {tuple(d["fact_ids"]) for d in diary} == {(quota,), (mix,)}
    assert store._query("SELECT kind FROM portrait_runs WHERE id=?", (result.run_id,))[0][0] == "bar"
    assert "save_portrait" in client.tools_called()             # re-rendered once

    # reversible: the whole pass taken back
    undone = BarPass(worker).undo(result.run_id)
    assert undone == {"restored": 2, "ended": 1, "diverged": []}
    current = {f.id: f for f in store.portrait_facts()}
    assert quota in current and mix in current and new.id not in current


def test_undo_leaves_a_retirement_whose_lasting_line_changed_since(store):
    """Review A2-5: a lasting line updated after the pass is not ended, and its retired fact is not restored beside
    it (that would say the same thing twice); the rest of the pass is taken back, and the gap reported."""
    from yuki.memory.dreaming import BarPass

    _j, ids = _facts(store)
    quota, mix = ids[0], ids[1]
    worker, _client = worker_with(store, {"judge_bar": _verdicts(ids), **RENDER})
    result = BarPass(worker).run(now=T0 + 7200)
    [new] = [f for f in store.portrait_facts() if f.text == LASTING]
    [(ok, newer)] = store.commit_portrait_changes(None, [FactChange(
        "UPDATE", new.id, "work", new.subject, LASTING + " They switch between them when one runs low.", 0.8,
        new.source_ids, T0 + 8000)])
    assert ok
    undone = BarPass(worker).undo(result.run_id)
    assert undone == {"restored": 1, "ended": 0, "diverged": [quota]}
    current = {f.id for f in store.portrait_facts()}
    assert newer in current and quota not in current and mix in current
    assert "left as they are" in store.changelog(pass_="bar")[0]["text"]


def test_a_saved_dry_run_is_applied_exactly_with_no_new_judgement(store, tmp_path):
    """Review A2-6: the dry run's decisions are kept (encrypted) and --apply writes exactly those; a fact changed
    since is skipped, and the judge is not called again."""
    from yuki.memory.dreaming import BarPass, load_plan, save_plan

    _j, ids = _facts(store)
    quota, mix = ids[0], ids[1]
    worker, client = worker_with(store, {"judge_bar": _verdicts(ids), **RENDER})
    dry = BarPass(worker).run(now=T0 + 7200, dry_run=True)
    path = save_plan(store, dry.decisions, tmp_path / "plan.json")
    raw = path.read_text(encoding="utf-8")
    assert "Claude accounts" not in raw and "enc:v1:" in raw          # the facts' words are not in the clear
    plan = load_plan(store, path)
    assert [d["id"] for d in plan] == [quota, mix] and plan[0]["lasting"] == LASTING and plan[0]["shown"]
    # the token-mix fact changed since the dry run: not written from the plan
    [(_ok, mix2)] = store.commit_portrait_changes(None, [FactChange("UPDATE", mix, "work", "token mix", MIX + " Again.",
                                                                    0.6, [], T0 + 7300)])
    judged = client.tools_called().count("judge_bar")
    result = BarPass(worker).run(now=T0 + 7400, plan=plan)
    assert result.ok, result.error
    assert client.tools_called().count("judge_bar") == judged          # no new judgement
    assert result.retired == 1 and result.lasting == 1 and any(f"F{mix}" in r for r in result.rejected)
    current = {f.id: f for f in store.portrait_facts()}
    assert quota not in current and mix2 in current
    assert [f for f in current.values() if f.text == LASTING][0].shown == plan[0]["shown"]
    with pytest.raises(ValueError):
        (tmp_path / "other.json").write_text('{"kind": "something else"}', encoding="utf-8")
        load_plan(store, tmp_path / "other.json")


# -- review fixes: the audit's operations, the gate's view of the portrait, dreaming's lasting lines -----------


def test_the_journal_taints_only_an_agents_usage_figures_not_its_project_reports():
    """Review A2-3: the AI-text rule covers an agent's usage, quota and spend figures and its advice on them; its
    reports about the user's own work stay usable (other_ai false)."""
    from yuki.memory.journal import OTHER_AI_GUIDANCE, system_prompt

    assert "usage, quota, limits or spend" in OTHER_AI_GUIDANCE
    assert "where a project stands" in OTHER_AI_GUIDANCE and "other_ai false as usual" in OTHER_AI_GUIDANCE
    assert "what it reports, measures, estimates" not in OTHER_AI_GUIDANCE      # the earlier, wider wording
    assert OTHER_AI_GUIDANCE.strip()[:60] in system_prompt(["Kai"])


def test_the_audit_prompt_holds_the_bar_and_its_operations_go_through_the_gate(store):
    """Review A2-2: the portrait audit's ADDs and UPDATEs meet the bar like a nightly run's."""
    from yuki.memory.portrait import AUDIT_SYSTEM_PROMPT, ADMISSION_BAR, RunResult
    from yuki.memory.store import PortraitRunStats

    assert ADMISSION_BAR in AUDIT_SYSTEM_PROMPT
    j = store.add_journal(T0, None, "Coding tool", "The usage panel showed the accounts' quota.", 5)
    [(_ok, mira)] = store.commit_portrait_changes(None, [FactChange("ADD", None, "person", "Mira",
                                                                    "Mira talks with the user.", 0.6, [j], T0)])

    def op(**kw):
        return {"op": "ADD", "fact_id": None, "kind": "work", "subject": "Claude", "text": QUOTA, "confidence": 0.7,
                "journal_ids": [j], "episode_ids": [], "based_on_facts": [], "reason": "x", "loop": None,
                "aspect": None, "finding": 1, "relationship": None, **kw}

    answer = {"findings": [{"finding": "relationship", "fact_ids": [mira], "journal_ids": [j], "decision": "resolve",
                            "reasoning": "x"}],
              "operations": [op(), op(op="UPDATE", fact_id=mira, kind="person", subject="Mira", text=SISTER)],
              "questions": []}
    worker, client = worker_with(store, {"judge_bar": [{"failing": [fail("N1", "usage_metrics")]}]})
    result = RunResult(None, "audit", True)
    stats = PortraitRunStats(kind="audit", model=worker.model)
    facts = store.portrait_facts()
    applied = worker._apply_audit(None, 1, answer, facts, store.journal_between(), T0 + 3600, result, stats,
                                  under=1, excerpts=0)
    assert applied == 1 and client.tools_called() == ["judge_bar"]
    assert [f.text for f in store.portrait_facts()] == [SISTER]
    assert result.operations[0].status == "rejected: fails the admission bar (usage_metrics)"
    assert stats.ops_rejected == 1 and stats.ops_update == 1


def test_the_bar_gate_sees_the_portrait_so_a_lasting_line_is_not_written_twice(store):
    """Review A2-4: the gate lists the current facts (not the ones this run updates) for the judge not to judge."""
    from yuki.memory.store import PortraitRunStats

    j = store.add_journal(T0, None, "Coding tool", "The usage panel showed the accounts' quota.", 5)
    written = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "Claude accounts", LASTING, 0.8, [j], T0),
        FactChange("ADD", None, "person", "Mira", "Mira talks with the user.", 0.6, [j], T0)])
    have, mira = [new for _ok, new in written]
    ops = _ops(j)[:1] + [{**_ops(j)[1], "op": "UPDATE", "fact_id": mira}]
    worker, client = worker_with(store, {"judge_bar": [{"failing": [fail("N1", "usage_metrics", None,
                                                                          "the lasting part is already a fact")]}]})
    facts = store.portrait_facts()
    journal = store.journal_between()
    changes, outcomes = worker._validate(ops, facts, journal, T0 + 3600)
    kept = worker._bar_gate(None, changes, outcomes, journal, PortraitRunStats(kind="nightly", model=worker.model),
                            facts=facts)
    assert [c.text for c in kept] == [SISTER]
    content = client.requests[-1]["messages"][0]["content"]
    other = content.split("OTHER FACTS", 1)[1]
    assert f"F{have} [work]" in other and f"F{mira} " not in other     # the updated fact is judged, not listed


def test_dreaming_keeps_the_lasting_part_of_a_fact_that_fails_the_bar(store):
    """Review A2-1: a bar retirement with durable points left goes on as those points (an UPDATE, history kept,
    citations, review and pin carried); one with nothing left ends; a lasting line said twice is written once."""
    from yuki.memory.dreaming import Dreamer, dream_system_prompt

    assert "nothing durable may be lost" in dream_system_prompt()
    j = [store.add_journal(T0 + i, None, "Coding tool", f"reading {i}", 5) for i in range(3)]
    written = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "Claude accounts", QUOTA, 0.8, [j[0], j[1]], T0, importance=6),
        FactChange("ADD", None, "work", "token mix", MIX, 0.6, [j[2]], T0),
        FactChange("ADD", None, "work", "accounts again", QUOTA + " Again.", 0.6, [j[2]], T0),
        FactChange("ADD", None, "person", "Mira", SISTER, 0.9, [j[2]], T0),
    ])
    quota, mix, again, sister = [new for _ok, new in written]
    store.mark_fact_reviewed(quota, T0 + 10)

    def retire(fid, text):
        return {"action": "retire", "fact_ids": [fid], "keep_id": None, "text": text, "importance": None,
                "journal_ids": [], "reason": "fails the admission bar: a tool's readings"}

    worker, _client = worker_with(store, {"dream": [{"decisions": [retire(quota, LASTING), retire(mix, ""),
                                                                   retire(again, LASTING)]}], **RENDER})
    result = Dreamer(worker, max_retire_fraction=1.0).run(now=T0 + 3600)
    assert result.ok and result.applied == 3, (result.error, result.rejected)
    current = {f.id: f for f in store.portrait_facts()}
    assert set(current) & {quota, mix, again} == set() and sister in current
    old = store.portrait_fact(quota)
    assert old.valid_to is not None and old.text == QUOTA          # kept as history
    [new] = [f for f in current.values() if f.text == LASTING]
    assert old.superseded_by == new.id and sorted(new.source_ids) == sorted([j[0], j[1]])
    assert new.importance == 6 and new.reviewed_at == T0 + 10
    assert store.portrait_fact(mix).superseded_by is None and store.portrait_fact(again).superseded_by is None
    lines = {tuple(d["fact_ids"]): d for d in store.changelog(pass_="dream")}
    assert lines[(quota,)]["new_fact_ids"] == [new.id] and "lasting part" in lines[(quota,)]["text"]


def test_a_dry_bar_pass_writes_nothing(store):
    from yuki.memory.dreaming import BarPass

    _j, ids = _facts(store)
    before = [(f.id, f.valid_to) for f in store.portrait_facts()]
    runs = store._query("SELECT count(*) FROM portrait_runs")[0][0]
    worker, client = worker_with(store, {"judge_bar": _verdicts(ids)})
    result = BarPass(worker).run(now=T0 + 7200, dry_run=True)
    assert result.ok and result.retired == 0 and len(result.decisions) == 2 and result.categories == {"usage_metrics": 2}
    assert [(f.id, f.valid_to) for f in store.portrait_facts()] == before
    assert store._query("SELECT count(*) FROM portrait_runs")[0][0] == runs and not store.changelog(pass_="bar")
    assert "save_portrait" not in client.tools_called()


def test_the_bar_pass_is_capped_and_chunked(store):
    from yuki.memory.dreaming import BarPass

    _j, ids = _facts(store)
    worker, client = worker_with(store, {"judge_bar": _verdicts(ids), **RENDER})
    result = BarPass(worker, max_retire_fraction=0.1, chunk_chars=400).run(now=T0 + 7200, dry_run=True)
    assert result.calls > 1                                   # several judge calls, every fact judged once
    listed = [r["messages"][0]["content"] for r in client.requests if r["tools"][0]["name"] == "judge_bar"]
    assert all("OTHER FACTS" in c for c in listed)
    assert len(result.decisions) == 1 and any("cap" in r for r in result.rejected)


# -- (4) opt-in: the real judge on the invented fixtures ---------------------------------------------------


@pytest.mark.skipif(os.environ.get("YUKI_LIVE_BAR") != "1", reason="live model check: set YUKI_LIVE_BAR=1")
def test_live_judge_holds_the_bar_on_invented_fixtures(store):
    """Costs about $0.02: one Sonnet call over seven invented facts."""
    from yuki.memory.dreaming import BarPass
    from yuki.memory.portrait import PortraitWorker

    _j, ids = _facts(store)
    quota, mix, sister, work, habit, advice, _loop = ids
    worker = PortraitWorker(store, log_dir=Path(tempfile.mkdtemp()))
    result = BarPass(worker).run(now=T0 + 7200, dry_run=True)
    assert result.ok, result.error
    failing = {d["id"] for d in result.decisions} | {p["id"] for p in result.pinned}
    assert {quota, mix, advice} <= failing
    assert not failing & {sister, work, habit}
    quota_line = next(d for d in result.decisions if d["id"] == quota)
    assert quota_line["lasting"] and not any(ch.isdigit() for ch in quota_line["lasting"])


@pytest.mark.skipif(os.environ.get("YUKI_LIVE_BAR") != "1", reason="live model check: set YUKI_LIVE_BAR=1")
def test_live_gate_holds_the_bar_on_invented_fixtures(store):
    """Costs about $0.01: the gate's one forced call over three invented new versions."""
    from yuki.memory.portrait import PortraitWorker
    from yuki.memory.store import PortraitRunStats

    j = store.add_journal(T0, None, "Coding tool", "The usage panel showed the accounts' quota.", 5)
    worker = PortraitWorker(store, log_dir=Path(tempfile.mkdtemp()))
    journal = store.journal_between()
    changes, outcomes = worker._validate(_ops(j), [], journal, T0 + 3600)
    kept = worker._bar_gate(None, changes, outcomes, journal, PortraitRunStats(kind="nightly", model=worker.model))
    texts = [c.text for c in kept]
    assert SISTER in texts and QUOTA not in texts and ADVICE not in texts
