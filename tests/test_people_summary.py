"""People summaries (report 55, appPeople): the person bar in every prompt that writes or judges facts, and the people
pass that rewrites each person's summary under it (pinned facts kept and reported, the pass reversible, a dry run's
plan applied exactly).

Stub models, temp databases, invented people and numbers. Opt-in live checks (``YUKI_LIVE_PEOPLE=1``) send the
invented fixtures to the real model.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.store import FactChange, Store

T0 = datetime(2026, 9, 30, 21, 0).timestamp()


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


RENDER = {"save_portrait": [{"text": "People", "profile": "", "personality": ""}]}

#: Invented people. Ravi's summary is the kind the user complained about: one call's talk, who shared what, and a
#: student portal's figures, around a few lasting points (cousin, from Nashik, studies architecture in Pune).
RAVI_BAD = ("Ravi is the user's cousin from Nashik who studies architecture in Pune; they talk on Discord. On a "
            "Discord call on 2026-09-30 the user said they had shared the lab notes with Ravi and that Ravi sent them "
            "a playlist. Ravi is currently in term 4 of 5 years, with 98 of 112 classes attended and total tuition of "
            "3,10,000.")
RAVI_MORE = "Ravi plays chess with the user most weekends."
RAVI_FEES = "Ravi's portal showed fees of 1,55,000 due and a CGPA of 8.4 on 2026-09-29."
RAVI_GOOD = ("Ravi is the user's cousin from Nashik and studies architecture in Pune, in the fourth year of a "
             "five-year course. They play chess most weekends and talk on Discord.")
RAVI_SHOWN = ("Ravi is your cousin from Nashik and studies architecture in Pune, in the fourth year of a five-year "
              "course. You play chess most weekends and talk on Discord.")
KIRAN = ("Kiran is the user's friend from school. He is from Pune and studies mechanical engineering there. They play "
         "Valorant together most nights and talk on Discord and WhatsApp; Kiran has semester exams in October 2026.")
MEERA_PINNED = ("Meera is the user's elder sister and a nurse in Eastvale. On 2026-09-28 at 21:14 she sent the user "
                "a photo of her ward roster on WhatsApp.")


def worker_with(store, answers):
    from yuki.memory.portrait import PortraitWorker

    client = ToolClient(answers)
    return PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp())), client


def add_fact(store, subject, text, *, pinned=False, origin="model", at=T0, importance=6):
    j = store.add_journal(at, None, "Discord", f"evidence about {subject}", 5)
    [(ok, fid)] = store.commit_portrait_changes(None, [FactChange(
        "ADD", None, "person", subject, text, 0.8, [j], at, origin, importance=importance,
        summary="Someone in your life", shown=text.replace("the user", "you"))])
    assert ok
    if pinned:
        store.set_fact_pinned(fid, True)
    return fid


def people(store):
    """Three invented people in the people index (Ravi with three person facts, Kiran, Meera pinned)."""
    from yuki.memory import contacts as C

    ravi = add_fact(store, "Ravi", RAVI_BAD)
    more = add_fact(store, "Ravi", RAVI_MORE, importance=4)
    fees = add_fact(store, "Ravi", RAVI_FEES, importance=3)
    kiran = add_fact(store, "Kiran", KIRAN)
    meera = add_fact(store, "Meera", MEERA_PINNED, pinned=True)
    C.apply(store, C.Linked(people=[
        {"id": None, "name": "Ravi", "relation": "cousin", "aliases": [], "tone": "", "facts": [ravi, more, fees],
         "routes": []},
        {"id": None, "name": "Kiran", "relation": "friend from school", "aliases": [], "tone": "", "facts": [kiran],
         "routes": []},
        {"id": None, "name": "Meera", "relation": "elder sister", "aliases": [], "tone": "", "facts": [meera],
         "routes": []}]), run_id=None, now=T0, rebuild=True)
    return {"ravi": ravi, "more": more, "fees": fees, "kiran": kiran, "meera": meera}


def answer(ids, *, meera_rewrite=True):
    """What a good judge says about the invented people (refs follow the index order: G1 Ravi, G2 Kiran, G3 Meera)."""
    return {"write_people": [{"people": [
        {"id": "G1", "rewrite": True, "text": RAVI_GOOD, "shown": RAVI_SHOWN, "label": "Cousin studying architecture in Pune",
         "dropped": ["person_moment", "person_figures"], "merged": [ids["more"]],
         "failing": [{"id": ids["fees"], "category": "person_figures", "reason": "a portal's figures"}],
         "reason": "call moments and portal figures"},
        {"id": "G2", "rewrite": False, "text": None, "shown": None, "label": None, "dropped": [], "merged": [],
         "failing": [], "reason": "meets the bar"},
        {"id": "G3", "rewrite": meera_rewrite,
         "text": "Meera is the user's elder sister and works as a nurse in Eastvale." if meera_rewrite else None,
         "shown": "Meera is your elder sister and works as a nurse in Eastvale." if meera_rewrite else None,
         "label": "Elder sister, a nurse in Eastvale" if meera_rewrite else None,
         "dropped": ["person_moment"] if meera_rewrite else [], "merged": [], "failing": [],
         "reason": "one message"},
    ]}], **RENDER}


# -- (1) the person bar in the prompts -------------------------------------------------------------------


def test_the_person_bar_is_in_every_prompt_that_writes_or_judges_facts():
    from yuki.memory.dreaming import dream_system_prompt
    from yuki.memory.people_summary import system_prompt
    from yuki.memory.portrait import (ADMISSION_BAR, AUDIT_SYSTEM_PROMPT, BAR_SYSTEM_PROMPT, OPS_SYSTEM_PROMPT,
                                      PERSON_BAR)

    assert ADMISSION_BAR.endswith(PERSON_BAR)
    for prompt in (OPS_SYSTEM_PROMPT, AUDIT_SYSTEM_PROMPT, BAR_SYSTEM_PROMPT, dream_system_prompt(), system_prompt()):
        assert PERSON_BAR in prompt
    # who, what, where, what they are like, the apps, what is going on; the user's own complaint as the negative
    for part in ("who they are to the user", "what they do", "where they are from", "what they are like",
                 "the apps", "what is going on", "classes attended", "tuition", "Discord call", "FAILS", "PASSES",
                 "Rewritten to what lasts"):
        assert part in PERSON_BAR
    assert PERSON_BAR.count("- FAILS:") >= 2 and PERSON_BAR.count("- PASSES:") >= 2
    assert "{" not in PERSON_BAR and "{" not in dream_system_prompt()


def test_the_shown_forms_and_labels_say_a_person_is_their_summary():
    from yuki.memory import titles

    assert titles.PERSON_SHOWN_RULE in titles.SHOWN_SYSTEM_PROMPT and titles.PERSON_SHOWN_RULE in titles.SHOWN_FIELD
    assert "never one moment, something shared once or a figure" in titles.SYSTEM_PROMPT


def test_the_bar_judge_knows_the_person_categories():
    from yuki.memory.portrait import BAR_CATEGORIES, parse_bar_verdicts

    assert {"person_moment", "person_figures"} <= set(BAR_CATEGORIES)
    got, bad = parse_bar_verdicts({"failing": [{"id": "N1", "category": "person_figures", "lasting": RAVI_GOOD,
                                                "shown": RAVI_SHOWN, "summary": "Cousin studying architecture",
                                                "reason": "portal figures"}]}, ["N1"])
    assert not bad and got[0].lasting == RAVI_GOOD


def test_a_route_use_is_what_the_chat_is_for_never_one_moment():
    from yuki.memory.contacts import LINK_TOOL, SYSTEM_PROMPT

    assert "never one moment" in SYSTEM_PROMPT
    use = LINK_TOOL["input_schema"]["properties"]["people"]["items"]["properties"]["routes"]["items"]["properties"]["use"]
    assert "never one moment" in use["description"]


# -- (2) person facts at the gate --------------------------------------------------------------------------


def test_the_gate_rewrites_a_person_update_that_carries_call_trivia(store):
    """A nightly UPDATE of a person fact that adds one call's talk and figures is written as the summary without
    them (the judge's lasting line), with the person bar in the judge's prompt."""
    from yuki.memory.portrait import OpOutcome
    from yuki.memory.store import PortraitRunStats

    ravi = add_fact(store, "Ravi", RAVI_GOOD)
    worker, client = worker_with(store, {"judge_bar": [{"failing": [{
        "id": "N1", "category": "person_moment", "lasting": RAVI_GOOD, "shown": RAVI_SHOWN,
        "summary": "Cousin studying architecture in Pune", "reason": "a call's talk and figures"}]}]})
    ch = FactChange("UPDATE", ravi, "person", "Ravi", RAVI_BAD, 0.8, [], T0 + 60, importance=6)
    out = OpOutcome("UPDATE", ravi, "person", "Ravi", RAVI_BAD, 0.8, [], [], "x")
    kept = worker._bar_gate(None, [ch], [out], [], PortraitRunStats(kind="nightly", model=worker.model))
    assert [c.text for c in kept] == [RAVI_GOOD] and kept[0].shown == RAVI_SHOWN
    assert "THE PERSON BAR" in client.requests[-1]["system"]


# -- (3) the people pass -----------------------------------------------------------------------------------


def test_people_are_grouped_by_the_index_with_the_summary_first(store):
    from yuki.memory.people_summary import people_of

    ids = people(store)
    loose = add_fact(store, "Anil", "Anil is the user's neighbour.")
    got = people_of(store, now=T0)
    assert [(p.ref, p.name, p.summary.id, [o.id for o in p.others]) for p in got] == [
        ("G1", "Ravi", ids["ravi"], [ids["more"], ids["fees"]]), ("G2", "Kiran", ids["kiran"], []),
        ("G3", "Meera", ids["meera"], []), ("G4", "Anil", loose, [])]
    assert got[0].relation == "cousin"


def test_people_verdicts_are_checked_against_the_listed_people(store):
    from yuki.memory.people_summary import parse_people, people_of

    ids = people(store)
    persons = people_of(store, now=T0)
    got, bad = parse_people({"people": [
        {"id": "G1", "rewrite": True, "text": RAVI_GOOD, "shown": RAVI_SHOWN, "label": "Cousin studying architecture",
         "dropped": ["person_moment", "made_up"], "merged": [ids["more"], ids["ravi"], ids["kiran"]],
         "failing": [{"id": ids["fees"], "category": "person_figures", "reason": "x"},
                     {"id": ids["kiran"], "category": "person_figures", "reason": "x"}], "reason": "x"},
        {"id": "G2", "rewrite": True, "text": "", "shown": None, "label": None, "dropped": [], "merged": [],
         "failing": [], "reason": "x"},
        {"id": "G2", "rewrite": False, "text": None, "shown": None, "label": None, "dropped": [], "merged": [],
         "failing": [], "reason": "again"},
        {"id": "G9", "rewrite": False, "text": None, "shown": None, "label": None, "dropped": [], "merged": [],
         "failing": [], "reason": "x"},
    ]}, persons)
    ravi, kiran = got
    assert ravi["merged"] == [ids["more"]] and [f["id"] for f in ravi["failing"]] == [ids["fees"]]
    assert ravi["dropped"] == ["person_moment"] and ravi["label"] == "Cousin studying architecture"
    assert kiran["rewrite"] is False      # a rewrite with no text is no rewrite
    # the summary and another person's fact are no merge; G2 twice, G9 unknown, G3 missing, G2's empty rewrite
    assert len(bad) == 7


def test_a_dry_run_decides_and_writes_nothing(store):
    from yuki.memory.people_summary import PeoplePass

    ids = people(store)
    before = {f.id: f.text for f in store.portrait_facts()}
    worker, client = worker_with(store, answer(ids))
    result = PeoplePass(worker).run(now=T0 + 3600, dry_run=True)
    assert result.ok, result.error
    assert result.run_id is None and result.calls == 1 and client.tools_called() == ["write_people"]
    assert {f.id: f.text for f in store.portrait_facts()} == before
    assert store.changelog(pass_="people") == []
    assert (result.people, result.rewritten, result.kept, result.retired) == (3, 1, 2, 2)
    assert result.dropped == {"person_moment": 1, "person_figures": 1}
    assert result.retired_why == {"merged": 1, "person_figures": 1}
    # Meera's summary is pinned: her rewrite is held back and reported, never written
    assert result.pinned == [{"id": ids["meera"], "what": "summary", "dropped": ["person_moment"]}]
    req = client.requests[0]["messages"][0]["content"]
    assert "UNTRUSTED_DATA" in req and f"F{ids['ravi']} SUMMARY" in req and f"F{ids['meera']} SUMMARY pinned" in req
    assert "relation \"cousin\"" in req


def test_the_pass_rewrites_summaries_keeps_pins_and_history_and_undoes(store):
    from yuki.memory import contacts as C
    from yuki.memory.people_summary import PeoplePass

    ids = people(store)
    worker, client = worker_with(store, answer(ids))
    result = PeoplePass(worker).run(now=T0 + 3600)
    assert result.ok, result.error
    assert (result.rewritten, result.retired) == (1, 2) and result.portrait
    assert client.tools_called() == ["write_people", "save_portrait"]
    current = {f.subject: f for f in store.portrait_facts(["person"])}
    assert set(current) == {"Ravi", "Kiran", "Meera"}
    ravi = current["Ravi"]
    assert ravi.text == RAVI_GOOD and ravi.shown == RAVI_SHOWN and ravi.summary == "Cousin studying architecture in Pune"
    # the merged fact's citations and rating went into the summary; the old version is history, superseded by it
    old = store.portrait_fact(ids["ravi"])
    assert old.valid_to is not None and old.superseded_by == ravi.id and old.text == RAVI_BAD
    assert set(store.portrait_fact(ids["more"]).source_ids) <= set(ravi.source_ids)
    assert store.portrait_fact(ids["more"]).valid_to is not None
    assert store.portrait_fact(ids["fees"]).valid_to is not None
    # pinned and unchanged facts kept as they were
    assert current["Meera"].id == ids["meera"] and current["Meera"].text == MEERA_PINNED and current["Meera"].pinned
    assert current["Kiran"].id == ids["kiran"] and current["Kiran"].text == KIRAN
    # the Me page's card follows the new version (the index still names the old id)
    contact = next(p for p in C.people(store) if p.name == "Ravi")
    card = C.card(store, contact, with_samples=0)
    assert card["summary"]["ref"] == f"fact:{ravi.id}" and card["summary"]["text"] == RAVI_SHOWN
    assert card["relation"] == "Cousin studying architecture in Pune" and card["facts_shown"] == []
    # one diary line per change, and a run row
    diary = store.changelog(pass_="people")
    assert sorted(d["action"] for d in diary) == ["retire", "retire", "rewrite"]
    runs = [dict(r) for r in store._query("SELECT kind, outcome, ops_update, ops_invalidate FROM portrait_runs "
                                          "WHERE id=?", (result.run_id,))]
    assert runs == [{"kind": "people", "outcome": "ok", "ops_update": 1, "ops_invalidate": 2}]

    undone = PeoplePass(worker).undo(result.run_id, now=T0 + 7200)
    assert undone == {"reverted": 1, "restored": 2, "diverged": []}
    texts = sorted(f.text for f in store.portrait_facts(["person"]))
    assert texts == sorted([RAVI_BAD, RAVI_MORE, RAVI_FEES, KIRAN, MEERA_PINNED])
    card = C.card(store, next(p for p in C.people(store) if p.name == "Ravi"), with_samples=0)
    assert card["summary"]["text"] == RAVI_BAD.replace("the user", "you")


def test_include_pinned_rewrites_a_pinned_summary_and_keeps_its_pin(store):
    from yuki.memory.people_summary import PeoplePass

    ids = people(store)
    worker, _client = worker_with(store, answer(ids))
    result = PeoplePass(worker).run(now=T0 + 3600, include_pinned=True)
    assert result.ok and result.rewritten == 2 and result.pinned == []
    meera = next(f for f in store.portrait_facts(["person"]) if f.subject == "Meera")
    assert meera.id != ids["meera"] and meera.pinned and "roster" not in meera.text


def test_a_pinned_other_fact_is_reported_not_retired(store):
    from yuki.memory.people_summary import PeoplePass

    ids = people(store)
    store.set_fact_pinned(ids["fees"], True)
    worker, _client = worker_with(store, answer(ids))
    result = PeoplePass(worker).run(now=T0 + 3600)
    assert result.ok and result.retired == 1
    assert {"id": ids["fees"], "what": "other", "dropped": ["person_figures"]} in result.pinned
    assert store.portrait_fact(ids["fees"]).valid_to is None


def test_a_saved_plan_is_applied_exactly_and_skips_what_changed(store):
    from yuki.memory.people_summary import PeoplePass, load_plan, save_plan

    ids = people(store)
    worker, client = worker_with(store, answer(ids))
    dry = PeoplePass(worker).run(now=T0 + 3600, dry_run=True)
    path = save_plan(store, dry.decisions, Path(tempfile.mkdtemp()) / "plan.json")
    assert RAVI_GOOD not in path.read_text(encoding="utf-8")            # encrypted at rest
    plan = load_plan(store, path)
    calls = len(client.requests)
    applied = PeoplePass(worker).run(now=T0 + 3600, plan=plan)
    assert applied.ok and (applied.rewritten, applied.retired) == (1, 2)
    assert client.tools_called()[calls:] == ["save_portrait"]           # no new judgement, one render
    # a second apply of the same plan: Ravi's summary is no longer the one it was decided on
    again = PeoplePass(worker).run(now=T0 + 3700, plan=plan)
    assert again.ok and again.rewritten == 0 and again.retired == 0
    assert any("no longer a current summary" in r for r in again.rejected)


def test_the_retirement_cap_holds(store):
    from yuki.memory.people_summary import PeoplePass

    ids = people(store)
    answers = answer(ids)
    ravi = answers["write_people"][0]["people"][0]
    ravi["merged"] = []
    ravi["failing"].append({"id": ids["more"], "category": "not_useful", "reason": "x"})
    worker, _client = worker_with(store, answers)
    result = PeoplePass(worker, max_retire_fraction=0.01).run(now=T0 + 3600, dry_run=True)
    assert result.retired == 1 and any("cap" in r for r in result.rejected)
    # merges are not capped: their points live on in the summary
    worker, _client = worker_with(store, answer(ids))
    assert PeoplePass(worker, max_retire_fraction=0.01).run(now=T0 + 3600, dry_run=True).retired == 2


def test_a_failed_call_writes_nothing_and_says_so(store):
    from yuki.memory.people_summary import PeoplePass

    people(store)
    before = {f.id for f in store.portrait_facts()}
    worker, _client = worker_with(store, {})
    result = PeoplePass(worker).run(now=T0 + 3600)
    assert not result.ok and result.error
    assert {f.id for f in store.portrait_facts()} == before


def test_a_stale_index_still_finds_the_current_summary(store):
    """The index names the version current at its last run; an UPDATE since (the nightly run) gave the fact a new id:
    the card follows it."""
    from yuki.memory import contacts as C

    ids = people(store)
    [(ok, new)] = store.commit_portrait_changes(None, [FactChange("UPDATE", ids["kiran"], "person", "Kiran", KIRAN + " x",
                                                                  0.8, [], T0 + 60, shown="new")])
    assert ok
    assert C.current_version(store, ids["kiran"]) == new
    card = C.card(store, next(p for p in C.people(store) if p.name == "Kiran"), with_samples=0)
    assert card["summary"]["ref"] == f"fact:{new}"


# -- live checks (opt-in) --------------------------------------------------------------------------------


LIVE = pytest.mark.skipif(os.environ.get("YUKI_LIVE_PEOPLE") != "1", reason="live model check: set YUKI_LIVE_PEOPLE=1")


@LIVE
def test_live_pass_writes_who_what_where_and_never_the_trivia(store):
    """Costs a few cents: one Sonnet call over the three invented people."""
    from yuki.memory.people_summary import PeoplePass
    from yuki.memory.portrait import PortraitWorker

    ids = people(store)
    worker = PortraitWorker(store, log_dir=Path(tempfile.mkdtemp()))
    result = PeoplePass(worker).run(now=T0 + 3600, dry_run=True)
    assert result.ok, result.error
    ravi = next(d for d in result.decisions if d["fact_id"] == ids["ravi"])
    assert ravi["rewrite"]
    for text in (ravi["text"], ravi["shown"]):
        low = text.lower()
        # who, what, where (invented specifics of the fixture)
        assert "cousin" in low and "nashik" in low and "architecture" in low
        # never the call's moments, who shared what, the portal's figures
        for bad in ("112", "98", "3,10,000", "1,55,000", "8.4", "playlist", "lab notes", "tuition", "attend",
                    "fees", "21:", "2026-09"):
            assert bad not in low, (bad, text)
    assert "you" in ravi["shown"].lower().split() or "your" in ravi["shown"].lower()
    assert ids["fees"] in {f["id"] for f in ravi["failing"]} | set(ravi["merged"])
    # Kiran's summary already meets the bar; Meera's is pinned: held back, reported
    assert not any(d["fact_id"] == ids["kiran"] and d["rewrite"] for d in result.decisions)
    assert ids["meera"] not in {d["fact_id"] for d in result.decisions if d["rewrite"]}
    assert ids["meera"] in {p["id"] for p in result.pinned}


@LIVE
def test_live_gate_holds_the_person_bar(store):
    """Costs about a cent: the gate's one forced call over an invented person update."""
    from yuki.memory.portrait import OpOutcome, PortraitWorker
    from yuki.memory.store import PortraitRunStats

    ravi = add_fact(store, "Ravi", "Ravi is the user's cousin from Nashik who studies architecture in Pune.")
    worker = PortraitWorker(store, log_dir=Path(tempfile.mkdtemp()))
    ch = FactChange("UPDATE", ravi, "person", "Ravi", RAVI_BAD, 0.8, [], T0 + 60, importance=6)
    out = OpOutcome("UPDATE", ravi, "person", "Ravi", RAVI_BAD, 0.8, [], [], "x")
    kept = worker._bar_gate(None, [ch], [out], [], PortraitRunStats(kind="nightly", model=worker.model))
    assert all("112" not in c.text and "playlist" not in c.text for c in kept)
    assert all("Nashik" in c.text for c in kept)
