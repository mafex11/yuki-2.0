"""Memory that cleans itself (report 76 item 5, yuki.memory.admission): what a call or a screen may put into memory now,
what waits for the user's yes, the user's answers, and the call's keep list.

Offline: temp databases, stub model clients, invented people (Tara, Kabir) and invented facts. Nothing here reads the
real database, a real model, the microphone or the desktop.
"""

from __future__ import annotations

import tempfile
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


def journal(store: Store, answers: dict):
    from yuki.memory.journal import JournalWorker

    client = ToolClient(answers)
    worker = JournalWorker(store, client=client, embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp()))
    return worker.run_once(), client


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
    assert A.route("user", "uncertain", None) is None                                    # never a fact anyway
    assert A.route("user", "fact", V("admit", "money", "")) == A.HOLD_SENSITIVE          # the judge's mark
    assert A.route("user", "humor", None, "sexuality") == A.HOLD_SENSITIVE               # the journal's mark


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
    assert visible == {"The user will send the hostel link after dinner.": "fact",
                       "The user bought generic prices.": "uncertain"}   # UNCLEAR: possibly misheard, never a fact
    held = {h.fact: h.reason for h in A.held_facts(store, now=T0 + 3600)}
    assert held == {"The user has five girlfriends.": A.HOLD_DROP,
                    "The user has been to Japan four times.": A.HOLD_CALL,
                    "Someone on the call said the trek starts at 6 AM on 11 October.": A.HOLD_CALL,
                    "The user said they started therapy in September.": A.HOLD_SENSITIVE}
    japan = next(h for h in A.held_facts(store, now=T0 + 3600) if "Japan" in h.fact)
    assert japan.shown == "You've been to Japan four times." and japan.call_id == cid
    # the judge's request carried the line each fact rests on; the judge's cost counts toward the batch
    judge_text = client.requests[1]["messages"][0]["content"]
    assert 'RESTS ON: THE USER: "bro relax, I have like five girlfriends"' in judge_text
    assert result.usage["input_tokens"] == 2000


def test_when_the_judge_fails_every_claim_waits(store):
    call_with(store, SCENE)
    [result], _client = journal(store, {"record_call_side": [CALL_ANSWER],
                                        "admit_call_facts": [RuntimeError("timeout")]})
    assert result.ok
    assert [e.fact for e in store.journal_between()] == ["The user bought generic prices."]   # uncertain anyway
    assert len(A.held_facts(store, now=T0 + 3600)) == 5


def test_a_screen_fact_marked_sensitive_waits_for_the_users_yes(store):
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, RECORD_FACTS_TOOL, system_prompt

    tid = store.upsert_thread("Google Chrome", "Northbank - Holdings", None, kind="page")
    store.add_capture(tid, T0, "foreground", "Holdings 12 funds", "Holdings 12 funds", kind="page")

    def sfact(text: str, sensitive: str) -> dict:
        return {"text": text, "nature": "fact", "importance": 5, "source": 1, "content_date": "", "milestone": False,
                "other_ai": False, "user_said": "no", "sensitive": sensitive}

    answer = {"facts": [sfact("The user checked the holdings in their Northbank brokerage account.", "money"),
                        sfact("The user had Google Chrome open.", "none")], "questions": [], "work": []}
    [result], _client = journal(store, {"record_facts": [answer]})
    assert result.ok and [e.fact for e in store.journal_between()] == ["The user had Google Chrome open."]
    [held] = A.held_facts(store, now=T0 + 60)
    assert held.reason == A.HOLD_SENSITIVE
    for tool in (RECORD_FACTS_TOOL, RECORD_CALL_SIDE_TOOL):
        item = tool["input_schema"]["properties"]["facts"]["items"]
        assert "sensitive" in item["required"] and item["properties"]["sensitive"]["enum"][0] == "none"
    assert "SENSITIVE ABOUT THE USER" in system_prompt(["Sam"]) and \
        "SENSITIVE ABOUT THE USER" in system_prompt(["Sam"], call_side=True)


# -- held facts: the user's answers -----------------------------------------------------------------------------


def held_fact(store: Store, text: str, reason: str | None, *, at: float = T0, call: int = 5, importance: int = 5,
              shown: str = "", nature: str = "fact") -> int:
    source = {"kind": "call", "by": "the user", "where": "Discord call", "at": at, "call": f"call:{call}"}
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


def test_a_held_fact_lapses_after_the_hold_days_and_a_rejournal_replaces_it(store):
    old = held_fact(store, "The user has a cat named Biscuit.", A.HOLD_CALL, at=T0 - (A.HOLD_DAYS + 1) * 86400)
    assert A.held_facts(store, now=T0) == [] and A.held_facts(store, since=0, now=T0)[0].id == old
    call_with(store, SCENE)
    journal(store, {"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]})
    first = {h.id for h in A.held_facts(store, now=T0 + 3600)}
    from yuki.memory.journal import JournalWorker

    [(capture, batch)] = store.journaled_captures(thread_kind="my_call_side")
    JournalWorker(store, client=ToolClient({"record_call_side": [CALL_ANSWER], "admit_call_facts": [JUDGE]}),
                  embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp())).rejournal(capture.thread_id, [capture],
                                                                                        [batch])
    second = {h.id for h in A.held_facts(store, now=T0 + 3600)}
    assert len(second) == len(first) and not first & second   # the re-journal's own held facts replace the old ones


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


def test_a_call_recap_with_waiting_lines_is_a_keep_card_written_without_a_model(store):
    from yuki.memory.api import MemoryClient
    from yuki.memory.nudges import NudgeConfig, NudgeWorker
    from yuki.memory.presence import PresenceWorker

    cid = call_with(store, [line(T0 + 10, "hello")])
    jid = held_fact(store, "The user has been to Japan four times.", A.HOLD_CALL, call=cid,
                    shown="You've been to Japan four times.")
    client = ToolClient({})
    worker = NudgeWorker(store, config=NudgeConfig(), client=client, log_dir=Path(tempfile.mkdtemp()),
                         locked_fn=lambda: False, media_fn=lambda: [], render_fn=lambda: [],
                         event_name="Local\\YukiNudgeReadyTestMemClean", clock=lambda: T0 + 3600)
    presence = PresenceWorker(worker)
    [call] = [c for c in store.calls(since=T0 - 60) if c.id == cid]
    look = presence._recap_look(f"recap:call:{cid}", [call], T0 + 3600)
    assert look.meta["keep"] == [{"ref": f"journal:{jid}", "text": "You've been to Japan four times."}]
    out = presence.observe(T0 + 3600, NudgeConfig(), None, [], look)
    assert out["nudge_id"] and client.requests == []                    # no model call writes it
    mem = MemoryClient(store)
    [card] = [n for n in mem.pending_nudges() if n["id"] == out["nudge_id"]] or \
        [mem._nudge_dict(store.nudge(out["nudge_id"]))]
    assert card["trigger"] == "recap" and card["kind"] == "notice"
    assert card["head"] == "From your Discord call: 1 thing I'd keep"
    assert card["keep"] == [{"ref": f"journal:{jid}", "text": "You've been to Japan four times."}]
    assert mem.recap_keep(out["nudge_id"], "journal:999999", "keep")["why"] == "not_on_card"
    assert mem.recap_keep(out["nudge_id"], f"journal:{jid}", "maybe")["why"] == "bad_answer"
    got = mem.recap_keep(out["nudge_id"], f"journal:{jid}", "keep")
    assert got["ok"] and got["nature"] == "fact"
    assert mem.recap_keep(out["nudge_id"], f"journal:{jid}", "keep")["why"] == "already"
    assert "The user has been to Japan four times." in {e.fact for e in store.journal_between()}
    assert presence._keep_look(f"recap:call:{cid}", [call], T0 + 3700) is None   # nothing waits any more


def test_held_facts_for_a_private_surface_never_list_dropped_ones(store):
    from yuki.memory.api import MemoryClient

    held_fact(store, "The user takes medication for migraines.", A.HOLD_SENSITIVE, at=1e10)
    held_fact(store, "The user is the best player in India.", A.HOLD_DROP, at=1e10)
    mem = MemoryClient(store)
    [item] = mem.held_facts(days=1e9)
    assert item["reason"] == "sensitive" and item["ref"].startswith("journal:")
    assert mem.resolve_held(item["ref"], "keep")["ok"]
    assert mem.held_facts(days=1e9) == []
