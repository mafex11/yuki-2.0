"""Memory that cleans itself (report 76 item 5, yuki.memory.admission): what a call or a screen may put into memory now,
what waits for the user's yes, the user's answers, and the call's keep list.

Offline: temp databases, stub model clients, invented people (Tara, Kabir) and invented facts. Nothing here reads the
real database, a real model, the microphone or the desktop.
"""

from __future__ import annotations

import tempfile
import time
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory import admission as A
from yuki.memory.store import ModelCall, NewFact, Store

T0 = datetime(2026, 10, 4, 23, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class StubEmbedder:
    model_name = "stub"

    def embed(self, texts):
        return [np.ones(8, dtype=np.float32) for _ in texts]


class TextEmbedder:
    """One vector per text (the same text, the same vector; different texts point different ways), like a real
    embedder for what the re-journal pairing reads."""
    model_name = "stub-text"

    def embed(self, texts):
        return [np.random.default_rng(zlib.crc32(t.encode("utf-8"))).standard_normal(16).astype(np.float32)
                for t in texts]


class ToolClient:
    """``messages.create`` answering the one tool of each request with a queued input (``Exception``: it raises)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        self.requests.append(request)
        name = request["tools"][0]["name"]
        queue = self.answers[name]
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(data, Exception):
            raise data
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))


def line(at: float, text: str, who: str = "user", **kw) -> dict:
    return {"at": at, "end": at + 3.0, "text": text, "lang": "en", "who": who, "app": "Discord", "track_kind": "call",
            **kw}


#: A Discord call (invented): a plan of the user's, a boast, a claim about their past, a friend's tease, a line the
#: recogniser was unsure of, and something sensitive.
SCENE = [
    line(T0 + 10, "can you send me the hostel link?", "others"),
    line(T0 + 14, "yeah I'll send it after dinner"),
    line(T0 + 30, "bro relax, I have like five girlfriends"),
    line(T0 + 50, "I've been to Japan like four times"),
    line(T0 + 70, "trek starts at six on Saturday the 11th", "others"),
    line(T0 + 90, "I bought the generic prices yesterday", unclear=True),
    line(T0 + 110, "I started therapy in September actually"),
]


def call_with(store: Store, lines: list[dict], start: float = T0) -> int:
    cid = store.start_call(app="Discord", process="discord.exe", via="mic", at=start, status="recording",
                           consent="always", consent_by="pref")
    store.update_call(cid, status="ended", ended_at=start + 600, stop_reason="mic_released",
                      recording_started_at=start)
    store.add_call_side(cid, app="Discord", process="discord.exe", started_at=start, ended_at=start + 600,
                        utterances=[u for u in lines if u["who"] == "user"], lines=lines, final=True)
    return cid


def cfact(text: str, source: int, nature: str = "fact", importance: int = 5, *, user: bool = True,
          sensitive: str = "none") -> dict:
    return {"text": text, "nature": nature, "importance": importance, "source": source, "user_said": user,
            "involves_user": True, "sensitive": sensitive}


CALL_ANSWER = {"facts": [
    cfact("The user will send the hostel link after dinner.", 2, importance=6),                 # admit
    cfact("The user has five girlfriends.", 3),                                                  # drop
    cfact("The user has been to Japan four times.", 4, importance=4),                            # ask
    cfact("Someone on the call said the trek starts at 6 AM on 11 October.", 5, user=False, importance=7),
    cfact("The user bought generic prices.", 6),                                                 # UNCLEAR
    cfact("The user said they started therapy in September.", 7),                                # sensitive (judge)
], "commitments": [], "questions": []}


def verdict(n: int, v: str, sensitive: str = "none", shown: str = "") -> dict:
    return {"fact": n, "verdict": v, "sensitive": sensitive, "shown": shown}


JUDGE = {"verdicts": [
    verdict(1, "admit"),
    verdict(2, "drop"),
    verdict(3, "ask", shown="You've been to Japan four times."),
    verdict(4, "admit", shown="Someone on the call said the trek starts at 6 AM on 11 October."),   # someone else's
    verdict(5, "ask"),
    verdict(6, "ask", "health", shown="You started therapy in September."),
]}


def journal(store: Store, answers: dict, embedder=None):
    from yuki.memory.journal import JournalWorker

    client = ToolClient(answers)
    worker = JournalWorker(store, client=client, embedder=embedder or StubEmbedder(),
                           log_dir=Path(tempfile.mkdtemp()))
    return worker.run_once(), client


def rejournal(store: Store, answers: dict, embedder=None) -> None:
    from yuki.memory.journal import JournalWorker

    [(capture, batch)] = store.journaled_captures(thread_kind="my_call_side")
    JournalWorker(store, client=ToolClient(answers), embedder=embedder or StubEmbedder(),
                  log_dir=Path(tempfile.mkdtemp())).rejournal(capture.thread_id, [capture], [batch])


# -- routing (structure + the judge's verdict) -------------------------------------------------------------------


def test_route_keeps_jokes_others_words_and_sensitive_facts_out_of_memory():
    V = A.Verdict
    assert A.route("user", "fact", V("admit", "none", "")) is None                       # their own plan: in now
    assert A.route("user", "fact", V("ask", "none", "")) == A.HOLD_CALL                  # a claim: waits
    assert A.route("user", "story", V("drop", "none", "")) == A.HOLD_DROP                # a joke taken literally
    assert A.route("others", "fact", V("admit", "none", "")) == A.HOLD_CALL              # never someone else's word
    assert A.route("others", "fact", V("drop", "none", "")) == A.HOLD_DROP
    assert A.route("user", "fact", None) == A.HOLD_CALL                                  # unjudged: waits
    assert A.route("user", "humor", V("drop", "none", "")) is None                       # a labelled joke stays a joke
    assert A.route("user", "fact", V("admit", "money", "")) == A.HOLD_SENSITIVE          # the judge's mark
    assert A.route("user", "humor", None, "sexuality") == A.HOLD_SENSITIVE               # the journal's mark
    # possibly misheard (review F2): held, not stored - unless the "Maybe misheard" list asks about it (its guess)
    assert A.route("user", "uncertain", V("admit", "none", "")) == A.HOLD_CALL
    assert A.route("user", "uncertain", None) == A.HOLD_CALL
    assert A.route("user", "uncertain", V("drop", "none", "")) == A.HOLD_DROP
    assert A.route("user", "uncertain", V("ask", "none", ""), listed=True) is None
    # someone else's words never go in, whatever the label; the judge's drop wins over the label (review F2)
    assert A.route("others", "uncertain", V("drop", "none", "")) == A.HOLD_DROP
    assert A.route("others", "uncertain", V("ask", "none", "")) == A.HOLD_CALL
    assert A.route("others", "uncertain", V("ask", "none", ""), listed=True) == A.HOLD_CALL
    assert A.route("others", "humor", None) == A.HOLD_CALL
    assert A.route("game", "fact", V("admit", "none", "")) == A.HOLD_CALL


def test_held_links_keep_work_and_questions_off_held_facts():
    facts = [SimpleNamespace(held=None), SimpleNamespace(held=A.HOLD_SENSITIVE), SimpleNamespace(held=A.HOLD_DROP)]
    ops = [SimpleNamespace(fact_indexes=[1]), SimpleNamespace(fact_indexes=[1, 2]), SimpleNamespace(fact_indexes=[0, 1]),
           SimpleNamespace(fact_indexes=[])]
    kept, left = A.unheld_links(facts, ops)
    assert kept == [ops[2], ops[3]] and left == 2


def test_the_judges_answer_is_checked():
    got = A.parse_verdicts({"verdicts": [verdict(1, "admit", "health"), verdict(1, "drop"), verdict(3, "maybe"),
                                         verdict(9, "ask"), {"fact": "x"}, verdict(2, "ask", "bogus", "x" * 500)]}, 3)
    assert got[0] == A.Verdict("admit", "health", "")       # the first answer for a fact stands
    assert got[1] == A.Verdict("ask", "none", "")           # an unknown kind is none; an overlong line is dropped
    assert got[2] is None                                    # an unknown verdict is no answer


def test_the_judge_sees_each_fact_with_its_line_and_the_lines_around_it_fenced():
    items = [A.JudgeItem("The user has five girlfriends.", "fact",
                         line(1, "bro relax, I have like five girlfriends"),
                         [(-1, line(0, "she said yes to the date?", "others")), (1, line(2, "hahaha", "others"))],
                         "Discord")]
    text = A.build_judge_message(items, "NONCE")
    assert "===BEGIN_UNTRUSTED_DATA_NONCE===" in text and text.index("before: OTHERS") < text.index("RESTS ON: THE USER")
    assert text.index("RESTS ON: THE USER") < text.index("after: OTHERS")
    assert "F1 [fact]: The user has five girlfriends." in text
    assert "UNCLEAR" in A.build_judge_message([A.JudgeItem("x", line=line(1, "y", unclear=True))], "N")
    client = ToolClient({"admit_call_facts": [RuntimeError("bedrock down")]})
    result = A.judge_call_facts(client, items)
    assert result.verdicts == [None] and "bedrock down" in result.error   # never raises; nothing judged
    prompt = client.requests[0]["system"][0]["text"]
    assert "SENSITIVE ABOUT THE USER" in prompt and "{identity}" not in prompt and "{sensitive}" not in prompt


# -- the journal: a call's facts ----------------------------------------------------------------------------------


def test_a_calls_plans_go_in_and_claims_jokes_others_words_and_sensitive_facts_wait(store):
    cid = call_with(store, SCENE)
    [result], client = journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    assert result.ok and [r["tools"][0]["name"] for r in client.requests] == ["record_call_side", "admit_call_facts"]
    visible = {e.fact: e.nature for e in store.journal_between()}
    assert visible == {"The user will send the hostel link after dinner.": "fact"}
    held = {h.fact: (h.reason, h.nature) for h in A.held_facts(store, now=T0 + 3600)}
    assert held == {"The user has five girlfriends.": (A.HOLD_DROP, "fact"),
                    "The user has been to Japan four times.": (A.HOLD_CALL, "fact"),
                    "Someone on the call said the trek starts at 6 AM on 11 October.": (A.HOLD_CALL, "fact"),
                    # UNCLEAR: possibly misheard - held, not stored (review F2)
                    "The user bought generic prices.": (A.HOLD_CALL, "uncertain"),
                    "The user said they started therapy in September.": (A.HOLD_SENSITIVE, "fact")}
    japan = next(h for h in A.held_facts(store, now=T0 + 3600) if "Japan" in h.fact)
    assert japan.shown == "You've been to Japan four times." and japan.call_id == cid
    # the judge's request carried the line each fact rests on; the judge's cost counts toward the batch
    judge_text = client.requests[1]["messages"][0]["content"]
    assert 'RESTS ON: THE USER: "bro relax, I have like five girlfriends"' in judge_text
    assert result.usage["input_tokens"] == 2000


def test_a_friends_tease_on_an_unclear_line_never_becomes_an_uncertain_fact_about_the_user(store):
    # review F2: the judge's drop and the others rule come before the uncertain label
    call_with(store, [line(T0 + 10, "he's literally addicted to energy drinks", "others", unclear=True),
                      line(T0 + 14, "shut up man")])
    tease = {"text": "The user drinks a lot of energy drinks.", "nature": "fact", "importance": 4, "source": 1,
             "user_said": False, "involves_user": True, "sensitive": "none"}
    [result], _client = journal(store, {"record_call_side": [{"facts": [tease], "commitments": [], "questions": []}],
                                        "admit_call_facts": [{"verdicts": [verdict(1, "drop")]}]})
    assert result.ok and store.journal_between() == []
    assert [(h.fact, h.reason) for h in A.held_facts(store, now=T0 + 3600)] == \
        [("The user drinks a lot of energy drinks.", A.HOLD_DROP)]


def test_when_the_judge_fails_the_batch_is_read_again_and_only_the_last_attempt_stores_unjudged(store):
    # review F3: an unjudged call holds even the user's own plans with no line a card can show - so try again first
    call_with(store, SCENE)
    answers = {"record_call_side": [CALL_ANSWER], "admit_call_facts": [RuntimeError("overloaded")]}
    [first], _client = journal(store, answers)
    assert not first.ok and "admission judge failed" in (first.error or "")
    assert store.journal_between() == [] and A.held_facts(store, now=T0 + 3600) == []
    [pending] = store.pending_captures()
    assert pending.attempts == 1                     # one attempt spent; read again on the next round
    [second], _client = journal(store, answers)
    assert not second.ok
    [third], _client = journal(store, answers)       # the last attempt: stored, every claim waiting
    assert third.ok and store.journal_between() == []
    held = A.held_facts(store, now=T0 + 3600)
    assert len(held) == 6 and not any(h.source.get(A.SHOWN_KEY) for h in held)


def test_after_one_failed_judge_the_next_round_admits_the_plans(store):
    call_with(store, SCENE)
    journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [RuntimeError("overloaded")]})
    [again], _client = journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    assert again.ok and [e.fact for e in store.journal_between()] == ["The user will send the hostel link after dinner."]


def test_when_the_judge_cannot_reach_the_model_the_batch_is_read_again_later(store):
    import anthropic
    import httpx

    call_with(store, SCENE)
    down = anthropic.APIConnectionError(request=httpx.Request("POST", "https://bedrock.invalid"))
    [result], _client = journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [down]})
    assert not result.ok and store.journal_between() == [] and A.held_facts(store, now=T0 + 3600) == []
    assert store.pending_captures()   # still waiting; no attempt spent on a network blip


def test_a_screen_fact_marked_sensitive_waits_for_the_users_yes(store):
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, RECORD_FACTS_TOOL, system_prompt

    tid = store.upsert_thread("Google Chrome", "Northbank - Holdings", None, kind="page")
    store.add_capture(tid, T0, "foreground", "Holdings 12 funds", "Holdings 12 funds", kind="page")
    store.add_capture(tid, T0 + 30, "foreground", "Relay docs: setup", "Relay docs: setup", kind="page")

    def sfact(text: str, sensitive: str, source: int = 1, shown: str = "") -> dict:
        return {"text": text, "nature": "fact", "importance": 5, "source": source, "content_date": "",
                "milestone": False, "other_ai": False, "user_said": "no", "sensitive": sensitive,
                "sensitive_shown": shown}

    def work(title: str, source: int) -> dict:
        return {"thread": 0, "action": "starts", "title": title, "state": "Started.", "open_items": [],
                "entities": [], "sources": [source]}

    answer = {"facts": [sfact("The user checked the holdings in their Northbank brokerage account.", "money",
                              shown="You checked your Northbank holdings."),
                        sfact("The user read the Relay setup docs.", "none", 2, shown="ignored when not sensitive")],
              "questions": [],
              # review F8: a thread resting only on the held fact is left out with it
              "work": [work("Northbank investments", 1), work("Relay setup", 2)]}
    [result], _client = journal(store, {"record_facts": [answer]})
    assert result.ok and [e.fact for e in store.journal_between()] == ["The user read the Relay setup docs."]
    [held] = A.held_facts(store, now=T0 + 60)
    assert held.reason == A.HOLD_SENSITIVE and held.shown == "You checked your Northbank holdings."
    assert A.SHOWN_KEY not in (store.journal_between()[0].source or {})
    assert [t.title for t in store.work_threads(("active", "paused", "done"))] == ["Relay setup"]
    for tool in (RECORD_FACTS_TOOL, RECORD_CALL_SIDE_TOOL):
        item = tool["input_schema"]["properties"]["facts"]["items"]
        assert "sensitive" in item["required"] and item["properties"]["sensitive"]["enum"][0] == "none"
    assert "SENSITIVE ABOUT THE USER" in system_prompt(["Sam"]) and \
        "SENSITIVE ABOUT THE USER" in system_prompt(["Sam"], call_side=True)


# -- held facts: the user's answers -----------------------------------------------------------------------------


def held_fact(store: Store, text: str, reason: str | None, *, at: float = T0, call: int = 5, importance: int = 5,
              shown: str = "", nature: str = "fact", who: str = "user") -> int:
    source = {"kind": "call", "by": "the user", "where": "Discord call", "at": at, "call": f"call:{call}"}
    if who == "others":
        source.update(who="others", by="someone on the call")
    if shown:
        source[A.SHOWN_KEY] = shown
    fact = NewFact(at=at, thread_id=None, app="Discord", host=None, fact=text, importance=importance,
                   vector=np.ones(8, dtype=np.float32), embed_model="stub", nature=nature, source=source, held=reason)
    store.commit_batch(ModelCall(at=at, thread_id=None, model="test"), [], [fact])
    return max(e.id for e in A._journal_rows(store, list(range(1, 10_000))))


def test_keep_joke_not_true_and_undo(store):
    kept = held_fact(store, "The user has been to Japan four times.", A.HOLD_CALL)
    joke = held_fact(store, "The user has five girlfriends.", A.HOLD_CALL)
    wrong = held_fact(store, "The user owns a boat.", A.HOLD_CALL)
    assert store.journal_between() == []
    k = A.resolve_held(store, kept, "keep", now=T0 + 100)
    j = A.resolve_held(store, joke, "joke", now=T0 + 100)
    n = A.resolve_held(store, wrong, "not_true", now=T0 + 100)
    assert k["new_id"] and j["new_id"] and n["new_id"] is None
    rows = {e.fact: e for e in store.journal_between()}
    assert set(rows) == {"The user has been to Japan four times.", "The user has five girlfriends."}
    assert rows["The user has five girlfriends."].nature == "humor" and rows["The user has been to Japan four times."].nature == "fact"
    assert store.journal_vectors([k["new_id"]])   # the vector came along: search finds it
    assert store.journal_after_id(wrong)          # the portrait's checkpoint sees the kept rows as new
    assert A.held_facts(store, now=T0 + 200) == []          # all answered
    assert A.resolve_held(store, kept, "keep", now=T0 + 300) is None   # already answered
    assert A.resolve_held(store, kept, "undo", now=T0 + 300)["answer"] == "undo"
    assert "The user has been to Japan four times." not in {e.fact for e in store.journal_between()}
    assert [h.id for h in A.held_facts(store, now=T0 + 400)] == [kept]   # offered again
    assert A.resolve_held(store, 999_999, "keep") is None
    diary = store.changelog(pass_=A.DIARY_PASS)
    assert len(diary) == 4 and all("held J" in d["text"] for d in diary)


def test_its_a_joke_on_someone_elses_line_stays_out_and_keep_marks_the_users_yes(store):
    from yuki.memory.portrait import PortraitWorker

    theirs = held_fact(store, "Someone on the call said the trek starts at 6 AM.", A.HOLD_CALL, who="others")
    misheard = held_fact(store, "The user bought generic prices.", A.HOLD_CALL, nature="uncertain")
    j = A.resolve_held(store, theirs, "joke", now=T0 + 100)   # review F5: a joke is the user's own humour only
    assert j["answer"] == "not_true" and j["new_id"] is None and store.journal_between() == []
    assert A.resolve_held(store, theirs, "undo", now=T0 + 110)["answer"] == "undo"   # nothing kept to take back
    k = A.resolve_held(store, misheard, "keep", now=T0 + 120)
    [kept] = store.journal_between()
    assert kept.id == k["new_id"] and kept.nature == "fact"    # a misheard line he keeps is a fact now
    assert kept.source[A.CONFIRMED_KEY] is True                # review F6: the portrait sees his yes
    line_text = PortraitWorker._journal_line(kept, lambda v, cap: str(v or "")[:cap])
    assert "[THE USER CONFIRMED THIS]" in line_text
    from yuki.memory.portrait import ADMISSION_BAR
    assert "THE USER CONFIRMED THIS" in ADMISSION_BAR


def test_a_rejournal_keeps_the_users_answers(store):
    # review F7: an answered held fact stays answered; the re-journal's fact on the same line carries the answer
    call_with(store, SCENE)
    journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    held = {h.fact: h for h in A.held_facts(store, now=T0 + 3600)}
    japan, trek = held["The user has been to Japan four times."], \
        held["Someone on the call said the trek starts at 6 AM on 11 October."]
    A.resolve_held(store, japan.id, "not_true", now=T0 + 3700)
    kept = A.resolve_held(store, trek.id, "keep", now=T0 + 3700)
    from yuki.memory.journal import JournalWorker

    admit_all = {"verdicts": [verdict(n, "admit") for n in range(1, 7)]}   # this time the judge would admit them
    [(capture, batch)] = store.journaled_captures(thread_kind="my_call_side")
    JournalWorker(store, client=ToolClient({"record_call_side": [CALL_ANSWER], "admit_call_facts": [admit_all]}),
                  embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp())).rejournal(capture.thread_id, [capture],
                                                                                        [batch])
    current = [e.fact for e in store.journal_between()]
    assert "The user has been to Japan four times." not in current               # Not true is not admitted again
    assert current.count("Someone on the call said the trek starts at 6 AM on 11 October.") == 1   # Keep: once
    assert kept["new_id"] in {e.id for e in store.journal_between()}
    assert A.resolve_held(store, japan.id, "undo", now=T0 + 3800)["answer"] == "undo"   # its Undo still works
    unanswered = {h.fact for h in A.held_facts(store, now=T0 + 3900)}
    assert "The user has been to Japan four times." in unanswered and len(
        [h for h in A.held_facts(store, now=T0 + 3900) if "Japan" in h.fact]) == 1


def test_a_held_fact_lapses_hold_days_after_it_was_held_and_a_rejournal_replaces_it(store):
    # review V5: the window runs from when the fact was held, never from its own date (a screen fact is dated by its
    # content: an old statement read today is held today and must be offered)
    old = held_fact(store, "The user has a cat named Biscuit.", A.HOLD_CALL, at=T0 - (A.HOLD_DAYS + 1) * 86400)
    held_at = time.time()
    assert [h.id for h in A.held_facts(store, now=held_at)] == [old]
    later = held_at + (A.HOLD_DAYS + 1) * 86400
    assert A.held_facts(store, now=later) == [] and A.held_facts(store, since=0, now=later)[0].id == old
    call_with(store, SCENE)
    journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    first = {h.id for h in A.held_facts(store, now=T0 + 3600)} - {old}
    rejournal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    second = {h.id for h in A.held_facts(store, now=T0 + 3600)} - {old}
    assert len(second) == len(first) == 5 and not first & second   # the re-journal's own held facts replace them


def test_a_sensitive_fact_with_an_old_date_is_offered_when_it_is_held(store):
    from yuki.memory.api import MemoryClient

    # review V5: a statement from 10 days ago read today is held today: the Me page offers it for the yes it needs
    fact = NewFact(at=time.time() - 10 * 86400, thread_id=None, app="Firefox", host=None,
                   fact="The user's card statement shows a missed payment.", importance=5,
                   vector=np.ones(8, dtype=np.float32), embed_model="stub", nature="fact",
                   source={"kind": "page", "by": "a page", "where": "Northbank", "at": time.time() - 10 * 86400},
                   held=A.HOLD_SENSITIVE)
    store.commit_batch(ModelCall(at=time.time(), thread_id=None, model="test"), [], [fact])
    [item] = MemoryClient(store).held_facts()
    assert item["reason"] == "sensitive" and item["fact"] == "The user's card statement shows a missed payment."


#: One user line carrying two facts: a plan the judge admits and a claim it asks about (review V2).
TREK = [line(T0 + 10, "you coming to the trek on Saturday?", "others"),
        line(T0 + 14, "yeah I'll book the bus tonight, I've done this trek like fifty times")]
PLAN, CLAIM = "The user will book the bus tickets tonight.", "The user has done this trek about fifty times."
TREK_ANSWER = {"facts": [cfact(PLAN, 2, importance=6), cfact(CLAIM, 2, importance=4)], "commitments": [],
               "questions": []}
TREK_JUDGE = {"verdicts": [verdict(1, "admit"), verdict(2, "ask", shown="You've done this trek about fifty times.")]}


def test_an_answer_carried_by_a_rejournal_never_takes_the_other_fact_on_its_line_out_of_memory(store):
    # review V2: Not true on the claim is carried to the claim only; the plan on the same line stays in memory
    call_with(store, TREK)
    emb = TextEmbedder()
    journal(store, {"record_call_side": [TREK_ANSWER], "admit_call_facts": [TREK_JUDGE]}, emb)
    assert [e.fact for e in store.journal_between()] == [PLAN]
    [claim] = A.held_facts(store, now=T0 + 3600)
    assert claim.fact == CLAIM
    A.resolve_held(store, claim.id, "not_true", now=T0 + 3700)
    rejournal(store, {"record_call_side": [TREK_ANSWER], "admit_call_facts": [TREK_JUDGE]}, emb)
    assert [e.fact for e in store.journal_between()] == [PLAN]                     # the plan stays in memory
    assert A.held_facts(store, now=T0 + 3800) == []                                # the claim stays answered
    # the facts come back in the other order and the judge would admit both this time: each still meets its own
    swapped = {"facts": TREK_ANSWER["facts"][::-1], "commitments": [], "questions": []}
    admit_both = {"verdicts": [verdict(1, "admit"), verdict(2, "admit")]}
    rejournal(store, {"record_call_side": [swapped], "admit_call_facts": [admit_both]}, emb)
    assert [e.fact for e in store.journal_between()] == [PLAN]
    answered = [h for h in A.held_facts(store, now=T0 + 3900, answered=True) if h.reviewed_at is not None]
    assert CLAIM in {h.fact for h in answered} and PLAN not in {h.fact for h in answered}


def test_without_vectors_the_pairing_keeps_held_with_held_and_the_place_on_the_line():
    key = ("call:1", 14.0, "user")
    # no vectors: a held fact meets the held one, an admitted one the admitted one
    assert Store._match_line_facts([(key, None, False), (key, None, True)],
                                   [(key, None, True), (key, None, False)]) == {0: 1, 1: 0}
    # all alike: the same place among the line's facts
    assert Store._match_line_facts([(key, None, False), (key, None, False)],
                                   [(key, None, False), (key, None, False)]) == {0: 0, 1: 1}
    # a fact off a call, or on another line, is never paired
    assert Store._match_line_facts([(None, None, True), (key, None, True)],
                                   [(None, None, True), (("call:1", 20.0, "user"), None, True)]) == {}


def test_a_sensitive_plan_is_still_a_to_do_and_card_lines_restate_the_fact():
    # review V1: the sensitive mark holds a fact, never the user's own to-do (call side, where to-dos are taken)
    from yuki.memory.journal import system_prompt

    assert A.SENSITIVE_COMMITMENTS_GUIDANCE in system_prompt(["Sam"], call_side=True)
    assert A.SENSITIVE_COMMITMENTS_GUIDANCE not in system_prompt(["Sam"])
    # review V6: a plan agreed to cites the user's answer; the judge's line restates the fact, never adds to it
    assert "never the OTHERS line that asked" in system_prompt(["Sam"], call_side=True)
    assert "shown restates the fact exactly as written" in A.CALL_ADMISSION_PROMPT


# -- the keep list -----------------------------------------------------------------------------------------------


def test_the_keep_list_offers_the_most_important_waiting_lines_never_sensitive_or_dropped(store):
    held_fact(store, "a", A.HOLD_CALL, importance=3, shown="You said a.")
    held_fact(store, "b", A.HOLD_CALL, importance=8, shown="You said b.")
    held_fact(store, "c", A.HOLD_SENSITIVE, importance=9, shown="You said c.")
    held_fact(store, "d", A.HOLD_DROP, importance=9)
    held_fact(store, "e", A.HOLD_CALL, importance=5, shown="You said e.")
    held_fact(store, "f", A.HOLD_CALL, importance=4, call=6, shown="You said f.")
    held_fact(store, "g", A.HOLD_CALL, importance=2, shown="You said g.")
    held_fact(store, "The user said h.", A.HOLD_CALL, importance=9)   # no second-person line: never on a card
    items = A.recap_items(store, [5], now=T0 + 60)
    assert [i["text"] for i in items] == ["You said b.", "You said e.", "You said a."]
    assert A.recap_items(store, [7], now=T0 + 60) == [] and A.recap_items(store, [], now=T0) == []
    assert A.recap_text(3, people=["Tara"]) == "From your call with Tara: 3 things I'd keep"
    assert A.recap_text(1, app="Discord") == "From your Discord call: 1 thing I'd keep"
    assert A.recap_text(2, people=["Tara", "Kabir", "Mira", "Dev"]) == \
        "From your call with Tara, Kabir and 2 others: 2 things I'd keep"
    assert A.recap_text(2) == "From your call: 2 things I'd keep"


def test_sensitive_matters_stay_out_of_the_call_digest():
    # review F4: when nothing waits, the model-written recap reads the digest, which never carries the matter. The old
    # recap writer's own "never anything personal and sensitive" rule went with NOTICE_WRITER_PROMPT (kProactive,
    # 18 Oct); the card judge writes the recap now.
    from yuki.memory.calldigest import system_prompt as digest_prompt

    assert "THE USER'S SENSITIVE MATTERS" in digest_prompt(["Sam"])


def test_held_facts_for_a_private_surface_never_list_dropped_ones(store):
    from yuki.memory.api import MemoryClient

    held_fact(store, "The user takes medication for migraines.", A.HOLD_SENSITIVE, at=1e10)
    held_fact(store, "The user is the best player in India.", A.HOLD_DROP, at=1e10)
    mem = MemoryClient(store)
    [item] = mem.held_facts(days=1e9)
    assert item["reason"] == "sensitive" and item["ref"].startswith("journal:")
    assert mem.resolve_held(item["ref"], "keep")["ok"]
    assert mem.held_facts(days=1e9) == []
