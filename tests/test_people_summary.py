"""People summaries (report 55, appPeople): the person bar in every prompt that writes or judges facts, the gate that
holds a person update to it, and the people index the Me card reads. (The one-off people pass,
``yuki.memory.people_summary``, was deleted on 18 Oct with its tests and its opt-in live checks.)

Stub models, temp databases, invented people and numbers.
"""

from __future__ import annotations

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


# -- (1) the person bar in the prompts -------------------------------------------------------------------


def test_the_person_bar_is_in_every_prompt_that_writes_or_judges_facts():
    from yuki.memory.dreaming import dream_system_prompt
    from yuki.memory.portrait import (ADMISSION_BAR, AUDIT_SYSTEM_PROMPT, BAR_SYSTEM_PROMPT, OPS_SYSTEM_PROMPT,
                                      PERSON_BAR)

    assert ADMISSION_BAR.endswith(PERSON_BAR)
    # the people pass's prompt (people_summary) went with kClean (18 Oct); every live writer and judge keeps the bar
    for prompt in (OPS_SYSTEM_PROMPT, AUDIT_SYSTEM_PROMPT, BAR_SYSTEM_PROMPT, dream_system_prompt()):
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


# -- (3) the people index ----------------------------------------------------------------------------------


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


# -- review fixes (report 55, "Review fixes") ---------------------------------------------------------------


def test_a_person_fact_added_since_the_link_run_joins_its_person(store):
    """Review F3: a person fact the nightly run added after the last link run, under a contact's name, is one of that
    contact's facts (after the cited ones), never a second person of the same name; the Me card shows it."""
    from yuki.memory import contacts as C

    ids = people(store)
    chess = add_fact(store, "Ravi", "Ravi is into chess.")
    # the people pass's own grouping (people_summary.people_of) went with kClean (18 Oct); the rule lives on in
    # contacts.person_facts_of, which the Me card reads
    card = C.card(store, next(p for p in C.people(store) if p.name == "Ravi"), with_samples=0)
    assert card["summary"]["ref"] == f"fact:{ids['ravi']}"
    assert f"fact:{chess}" in {f["ref"] for f in card["facts_shown"]}
