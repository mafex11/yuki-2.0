"""Offline checks of the OpenClaw-inspired memory work (docs/MEMORY.md, "Fast memory, provenance, dreaming").

Stub models and embedders, temp databases: fast memory (1), deep recall (2), provenance (3),
dreaming (4), the AI-tool taint (5), the portrait's separate budgets (6), Yuki's persona (7),
standing requests (8) and know-how from corrections (9).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory.journal import JournalWorker, record_facts_tool, system_prompt
from yuki.memory.store import FactChange, NewNudge, NudgeCheckin, Store

T0 = datetime(2026, 9, 25, 4, 0).timestamp()
DIM = 8


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def unit(i: int, mix: float = 0.0, j: int | None = None) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    if j is not None:
        v[j] = mix
    return v / np.linalg.norm(v)


class StubEmbedder:
    """Embeds a text as the axis its first word names ("a0 ..." -> e0), counting calls."""

    model_name = "stub"

    def __init__(self):
        self.calls = 0

    def embed(self, texts, wait_s=None):
        self.calls += 1
        out = []
        for t in texts:
            head = (t.split() or ["a0"])[0]
            i = int(head[1]) if len(head) == 2 and head[0] == "a" and head[1].isdigit() else 7
            out.append(unit(i))
        return np.stack(out)


def usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0)


#: The admission judge (yuki.memory.admission) admitting every call fact, so these checks see them in the journal.
ADMIT_ALL = {"verdicts": [{"fact": n, "verdict": "admit", "sensitive": "none", "shown": ""} for n in range(1, 41)]}


class ToolClient:
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


def slack_capture(store: Store, text: str = "Varun: approved, go ahead with the scope") -> int:
    tid = store.upsert_thread("Slack", "Varun (DM) - Slack", None, scope="Varun (DM)", kind="page")
    store.add_capture(tid, T0, "foreground", text, text, kind="page")
    return tid


def fact(text, source=1, other_ai=False, importance=5, user_said="no"):
    return {"text": text, "nature": "fact", "importance": importance, "source": source, "content_date": "",
            "milestone": False, "other_ai": other_ai, "user_said": user_said}


# -- (3) provenance and (5) taint at write time ------------------------------------------------


def test_journal_stores_provenance_and_the_ai_tool_taint(store):
    slack_capture(store)
    answer = {"facts": [fact("Varun approved the scope on Slack."),
                        fact("The user is a Spanish teacher.", other_ai=True)], "questions": []}
    worker = JournalWorker(store, client=ToolClient({"record_facts": [answer]}), embedder=StubEmbedder(),
                           log_dir=Path(tempfile.mkdtemp()))
    [result] = worker.run_once()
    assert result.ok
    rows = store.journal_between()
    assert rows[0].source["kind"] == "page" and "Varun (DM) in Slack" in rows[0].source["where"]
    assert rows[1].tainted and not rows[0].tainted
    # the prompt carries the guidance; the call side never gets it (the user's own voice)
    assert "TEXT FOR OR FROM ANOTHER AI TOOL" in system_prompt(["S"])
    assert "TEXT FOR OR FROM ANOTHER AI TOOL" not in system_prompt(["S"], call_side=True)
    from yuki.memory.personality import usable

    assert usable(rows[0]) and not usable(rows[1])      # the coach, episodes and the review leave it out


def test_the_users_words_need_a_source_the_user_wrote(store):
    """A local test page's "You:" line is never the user's words (2026-10-01, J1589); every capture names its page."""
    url = "file:///C:/Users/x/AppData/Local/Temp/yuki-tts/agent/index.html"
    tid = store.upsert_thread("Google Chrome", "Yuki voice samples", url, kind="page")
    text = "You: dude I just got the offer from Google!\nYuki (as written): Oh my god, no way!"
    store.add_capture(tid, T0, "focus", text, text, kind="page")
    answer = {"facts": [fact("The user told Yuki they had just received a job offer from Google.",
                             user_said="to_person_or_yuki"),
                        fact("The user had a local test page 'Yuki voice samples' open in Chrome."),
                        fact("The user asked ChatGPT for a Spanish script.", user_said="to_ai_tool")], "questions": []}
    client = ToolClient({"record_facts": [answer]})
    worker = JournalWorker(store, client=client, embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp()))
    [result] = worker.run_once()
    assert result.ok and result.facts == ["The user had a local test page 'Yuki voice samples' open in Chrome.",
                                          "The user asked ChatGPT for a Spanish script."]   # an AI tool's prompt stays
    sent = client.requests[0]["messages"][0]["content"]
    assert 'CAPTURE' in sent and f'from page "Yuki voice samples" at {url} (a file on this PC, not a website)' in sent
    assert "user_said" in record_facts_tool()["input_schema"]["properties"]["facts"]["items"]["required"]
    # the same flag on the user's own message is kept: the source's author is the user
    mine = SimpleNamespace(at=T0, utterance=None, command=None, item=None, capture=SimpleNamespace(by_yuki=False),
                           message=SimpleNamespace(is_me=True, sender="Kian", text="I got the offer!",
                                                   status="new"))
    said = {"facts": [fact("The user told Varun they got the offer.", user_said="to_person_or_yuki")]}
    reply = SimpleNamespace(stop_reason="tool_use",
                            content=[SimpleNamespace(type="tool_use", name="record_facts", input=said)])
    dropped: list[int] = []
    kept = worker._parse_facts(reply, store.thread_info(tid), [mine], not_users_words=dropped)
    assert [f.fact for f in kept] == ["The user told Varun they got the offer."] and dropped == []


def test_a_question_whose_evidence_was_rejournaled_is_vetted_on_the_new_reading(store):
    """Q28 (2026-10-01): its evidence, a misread iCloud Drive listing, was re-journaled; the vetter must see that."""
    from yuki.memory import questions as Q
    from yuki.memory.store import NewQuestion

    tid = store.upsert_thread("Google Chrome", "iCloud Drive", "https://www.icloud.com/iclouddrive/", kind="page")
    row = "Offer Letter - Kian PDF document 199 KB 30/09/2026, 19:00"
    store.add_capture(tid, T0, "title", row, row, kind="page")
    first = {"facts": [fact("The user received an offer letter from Corvana.", importance=9)], "questions": []}
    JournalWorker(store, client=ToolClient({"record_facts": [first]}), embedder=StubEmbedder(),
                  log_dir=Path(tempfile.mkdtemp())).run_once()
    [old] = store.journal_between()
    [(qid, _)] = store.add_questions([NewQuestion(
        at=T0, kind="fact", subject="offer letter", question="Has the user accepted the offer letter?",
        evidence="iCloud Drive", reason="", importance=8, origin="journal", source_ids=[old.id])])
    safe = lambda v, cap: str(v)[:cap]   # noqa: E731
    assert Q.reread_items(store, store.question(qid), safe) is None      # its evidence still stands
    again = {"facts": [fact("iCloud Drive listed a PDF named 'Offer Letter - Kian'.", importance=4)],
             "questions": []}
    caps = [c for c, _ in store.journaled_captures(batch_ids={old.batch_id})]
    JournalWorker(store, client=ToolClient({"record_facts": [again]}), embedder=StubEmbedder(),
                  log_dir=Path(tempfile.mkdtemp())).rejournal(tid, caps, [old.batch_id])
    replaced, of, items = Q.reread_items(store, store.question(qid), safe)
    assert (replaced, of) == (1, 1) and "listed a PDF named 'Offer Letter - Kian'" in items[0].line
    text, shown = Q.vet_message(store, [store.question(qid)], [None], "N")
    assert "ITS EVIDENCE WAS READ AGAIN" in text and items[0].ref in shown


def test_call_facts_carry_the_users_words_and_describe_says_so(store):
    from yuki.memory.provenance import describe

    utt = [{"at": T0 + 10, "end": T0 + 14, "text": "that would be the biggest update", "lang": "en"},
           {"at": T0 + 20, "end": T0 + 24, "text": "CLI access will connect over MCP over Chrome", "lang": "en"}]
    store.add_call_side(6, app="Discord", process="discord", started_at=T0, ended_at=T0 + 60, utterances=utt,
                        final=True)
    answer = {"facts": [{"text": "The user was setting up CLI access with MCP.", "nature": "fact", "importance": 5,
                         "source": 2}], "commitments": [], "questions": []}
    JournalWorker(store, client=ToolClient({"record_call_side": [answer], "admit_call_facts": [ADMIT_ALL]}), embedder=StubEmbedder(),
                  log_dir=Path(tempfile.mkdtemp())).run_once()
    [e] = store.journal_between()
    assert e.source["kind"] == "call" and e.source["where"] == "Discord call"
    assert "that would be the biggest update … CLI access will connect over MCP" in e.source["quote"]
    line = describe(e.source)
    assert line.startswith("Discord call, Fri 25 Sep 04:00") and "the user's own words" in line


def test_provenance_backfill_recovers_the_source_by_its_time(store):
    from yuki.memory.provenance import backfill

    slack_capture(store)
    JournalWorker(store, client=ToolClient({"record_facts": [{"facts": [fact("Varun approved.")], "questions": []}]}),
                  embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp())).run_once()
    store._query("UPDATE journal SET source_ciphertext=NULL")
    store._conn.commit()
    report = backfill(store, embed=StubEmbedder().embed)
    assert report["found"]["time"] == 1 and report["written"] == 1
    assert store.journal_between()[0].source["kind"] == "page"


# -- (8) standing requests ---------------------------------------------------------------------


def test_standing_request_fires_one_card_from_the_journals_own_call(store):
    from yuki.memory.api import MemoryClient

    client_api = MemoryClient(store)
    ref = client_api.add_standing_request("Varun replies to the user", "tell me when Varun replies")
    rid = int(ref.split(":")[1])
    slack_capture(store)
    answer = {"facts": [fact("Varun approved the scope on Slack.")], "questions": [],
              "request_matches": [{"request": rid, "source": 1, "why": "Varun replied",
                                   "message": "Varun replied on Slack: approved, go ahead with the scope"},
                                  {"request": 999, "source": 1, "why": "x", "message": "x"}]}
    tool = ToolClient({"record_facts": [answer]})
    worker = JournalWorker(store, client=tool, embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp()))
    worker.run_once()
    sent = tool.requests[0]
    from yuki.memory.journal import system_text

    assert "OPEN REQUESTS" in sent["messages"][0]["content"] and "OPEN REQUESTS" in system_text(sent["system"])
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}      # the fixed prompt is cached
    assert "request_matches" in sent["tools"][0]["input_schema"]["required"]
    cards = store.nudges(kinds=["reminder"])
    assert len(cards) == 1 and cards[0].ref == f"request:{rid}" and "Varun replied" in cards[0].text
    assert store.standing_request(rid)["status"] == "fired"
    # no open request: the batch runs exactly as before (no block, no field)
    assert record_facts_tool(False)["input_schema"]["required"] == ["facts", "questions", "work"]   # threads of work (23)


def test_standing_request_cooldown_budget_and_expiry_are_code(store):
    rid = store.add_standing_request("x happens", "tell me every time x happens", max_fires=3, cooldown_s=600,
                                     at=T0)
    c = lambda at: NudgeCheckin(at=at, trigger="request", outcome="reminder")   # noqa: E731
    n = NewNudge(kind="reminder", text="x happened", ref=f"request:{rid}")
    assert store.fire_standing_request(rid, c(T0 + 1), n, now=T0 + 1)[1] == "fired"
    assert store.fire_standing_request(rid, c(T0 + 60), n, now=T0 + 60)[1] == "cooldown"
    assert store.fire_standing_request(rid, c(T0 + 700), n, now=T0 + 700, daily_budget=1,
                                       day_start=T0)[1] == "daily budget"
    assert store.fire_standing_request(rid, c(T0 + 800), n, now=T0 + 800)[1] == "fired"
    assert store.fire_standing_request(rid, c(T0 + 1500), n, now=T0 + 1500)[1] == "fired"
    assert store.standing_request(rid)["status"] == "fired"          # its budget of 3 is spent
    old = store.add_standing_request("y", "tell me when y", expires_at=T0 + 10, at=T0)
    assert all(r["id"] != old for r in store.standing_requests(("open",), now=T0 + 20))


def test_watch_for_tool_checks_the_users_words():
    from yuki.agent.memory import MemoryAccess

    saved = {}

    class Client:
        def add_standing_request(self, condition, quote, request=None, repeat=False, days=None):
            saved.update(condition=condition, quote=quote)
            return "request:1"

    access = MemoryAccess(open_client=lambda: Client())
    bad = access.run_tool("watch_for", {"action": "add", "condition": "Varun replies", "quote": "notify me"},
                          request="tell me when Varun replies")
    assert not bad.ok and not saved
    good = access.run_tool("watch_for", {"action": "add", "condition": "Varun replies",
                                         "quote": "tell me when Varun replies"}, request="tell me when Varun replies")
    assert good.ok and saved["quote"] == "tell me when Varun replies" and good.payload["ref"] == "request:1"


# -- (1) fast memory -----------------------------------------------------------------------------


def seed_index(store: Store) -> dict[str, int]:
    ids = {}
    [(ok, pf)] = store.commit_portrait_changes(None, [FactChange("ADD", None, "person", "Varun", "a1 Varun is the CEO.",
                                                                 0.9, [], T0, importance=8)])
    store.add_portrait_fact_vectors([(pf, unit(1))], "stub")
    ids["portrait"] = pf
    for key, axis, nature, tainted in (("cli", 2, "fact", 0), ("doubt", 3, "uncertain", 0), ("ai", 4, "fact", 1)):
        jid = store.add_journal(T0, None, "Discord", f"a{axis} fact {key}", 6)
        store._query("UPDATE journal SET nature=?, tainted=? WHERE id=?", (nature, tainted, jid))
        store.add_vectors([(jid, unit(axis))], "stub")
        ids[key] = jid
    store._conn.commit()
    return ids


def test_relevant_ranks_filters_and_follows_changes(store):
    from yuki.memory.relevant import RelevantIndex

    ids = seed_index(store)
    index = RelevantIndex(store)
    index.refresh()
    assert [h["id"] for h in index.search(unit(1), now=T0)] == [ids["portrait"]]
    assert [h["id"] for h in index.search(unit(2), now=T0)] == [ids["cli"]]
    assert index.search(unit(3), now=T0) == [] and index.search(unit(4), now=T0) == []   # uncertain, other-AI text
    assert index.search(unit(6), now=T0) == []                                             # nothing close enough
    # a near-duplicate is left out; importance breaks the tie
    jid = store.add_journal(T0, None, "Discord", "a1 again", 2)
    store.add_vectors([(jid, unit(1, 0.05, 5))], "stub")
    store._conn.commit()
    index.refresh()
    hits = index.search(unit(1), now=T0)
    assert [h["id"] for h in hits] == [ids["portrait"]]
    # a superseded journal fact and an ended portrait fact drop out on the next refresh
    store._query("UPDATE journal SET superseded_at=? WHERE id=?", (T0, ids["cli"]))
    store.commit_portrait_changes(None, [FactChange("INVALIDATE", ids["portrait"], "person", "Varun", "", None, [], T0)])
    store._conn.commit()
    index.refresh()
    assert index.search(unit(2), now=T0) == []
    assert [h["id"] for h in index.search(unit(1), now=T0)] == [jid]


def test_relevant_and_knowhow_share_one_embedding_and_carry_provenance(store):
    from yuki.memory.api import MemoryClient

    ids = seed_index(store)
    client = MemoryClient(store)
    emb = StubEmbedder()
    client._embedder = emb
    client.knowhow(app=None, query="a2 what's the CLI setup", limit=4, embed_wait_s=0.5)
    hits = client.relevant("a2 what's the CLI setup", embed_wait_s=0.5, now=T0)
    assert emb.calls == 1
    assert hits[0]["id"] == ids["cli"] and hits[0]["source"]


def test_agent_blocks_carry_where_each_memory_came_from():
    from yuki.agent.memory import format_recall, relevant_block_text

    block = relevant_block_text([{"kind": "fact", "text": "The user set up CLI access with MCP.", "nature": "fact",
                                  "source": 'Discord call, Fri 25 Sep 04:26; the user\'s own words: "CLI access..."'}])
    assert block.startswith("[Memories that may bear on this message") and "- (Discord call" in block
    assert relevant_block_text([]) is None
    out = format_recall([{"kind": "fact", "at": "2026-09-25T04:26", "fact": "X", "source": "Discord call ...",
                          "other_ai": True}], query="x")
    assert "from: Discord call" in out and "another AI tool" in out


# -- (2) deep recall -------------------------------------------------------------------------------


def test_recall_with_time_use_reconciles_facts_and_the_timeline():
    from yuki.agent.memory import MemoryAccess

    class Client:
        def recall(self, q, since=None, until=None, app=None, limit=15):
            return [{"kind": "fact", "at": "2026-09-25T14:05", "fact": "The user said the build failed.",
                     "source": "Discord call, Fri 25 Sep 14:05"}]

        def activity(self, since=None, until=None, group_by="app", limit=15):
            return {"items": [], "totals": {"present_s": 3600}, "calls": [{"text": "Discord call Fri 13:20-02:33"}]}

        def episodes(self, since=None, until=None, limit=20):
            return [{"start": "2026-09-25T13:20", "end": "2026-09-25T15:00", "text": "coding in Warp"}]

    access = MemoryAccess(open_client=lambda: Client())
    assert not access.run_tool("recall", {"query": "friday", "with_time_use": True}).ok    # needs a window
    out = access.run_tool("recall", {"query": "what did I do", "since": "2026-09-25T12:00", "until": "2026-09-25T18:00",
                                     "with_time_use": True})
    text = out.content[0]["text"]
    assert out.ok and "Measured time use in the same window" in text and "Discord call Fri 13:20" in text
    assert "The user said the build failed." in text


# -- (5)+(6) the portrait ---------------------------------------------------------------------------


def test_portrait_rejects_ai_tool_text_and_keeps_importance(store):
    from yuki.memory.portrait import PortraitWorker

    ai = store.add_journal(T0, None, "Google Chrome", "The user is a Spanish teacher.", 5)
    real = store.add_journal(T0, None, "Slack", "The user works at Corvana.", 6)
    store._query("UPDATE journal SET tainted=1 WHERE id=?", (ai,))
    store._conn.commit()
    worker = PortraitWorker(store, client=object())
    base = {"op": "ADD", "fact_id": None, "subject": "", "confidence": 0.5, "episode_ids": [], "based_on_facts": [],
            "reason": "", "loop": None, "aspect": None}
    items = [{**base, "kind": "work", "text": "Spanish teacher.", "journal_ids": [ai], "importance": 6},
             {**base, "kind": "work", "text": "Works at Corvana.", "journal_ids": [real], "importance": 8}]
    changes, outcomes = worker._validate(items, [], store.journal_between(), T0 + 60)
    assert outcomes[0].status.startswith("rejected: rests only on text for or from another AI tool")
    assert outcomes[1].status == "applied" and changes[0].importance == 8
    [(ok, fid)] = store.commit_portrait_changes(None, changes)
    assert store.portrait_fact(fid).importance == 8


def test_profile_sections_have_their_own_budget(store):
    from yuki.memory.personality import split_personality
    from yuki.memory.portrait import MAX_PROFILE_CHARS, PortraitWorker

    jid = store.add_journal(T0, None, "Slack", "x", 5)
    store.commit_portrait_changes(None, [FactChange("ADD", None, "work", "w", "Works.", 0.8, [jid], T0),
                                         FactChange("ADD", None, "person", "Varun", "Varun is the CEO.", 0.8, [jid], T0)])
    huge = "Work\n" + "\n".join(f"- project {i} " + "x" * 80 for i in range(200))
    client = ToolClient({"save_portrait": [{"text": huge, "profile": "People\n- Varun: the CEO.\nPreferences\n- Arc.",
                                            "personality": ""}]})
    text = PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp())).render()
    rest, _ = split_personality(text)
    assert "People\n- Varun: the CEO." in rest and "Preferences" in rest     # never squeezed out by work
    assert len(rest) < 6000 + MAX_PROFILE_CHARS


# -- (4) dreaming --------------------------------------------------------------------------------------


def test_dreaming_merges_keeps_citations_caps_and_writes_the_diary(store):
    from yuki.memory.dreaming import Dreamer
    from yuki.memory.portrait import PortraitWorker

    j = [store.add_journal(T0 + i, None, "Discord", f"entry {i}", 5) for i in range(6)]
    facts = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "personality", "swearing", "Swears casually with friends.", 0.5, [j[0]], T0, aspect="talk"),
        FactChange("ADD", None, "personality", "swearing 2", "Swears a lot in Hinglish.", 0.5, [j[1], j[2]], T0,
                   aspect="talk"),
        FactChange("ADD", None, "interest", "quix", "quix is on a break (June 2025).", 0.4, [j[3]], T0),
        FactChange("ADD", None, "work", "Corvana", "Works at Corvana.", 0.9, [j[4]], T0, origin="user"),
        FactChange("ADD", None, "open_loop", "Varun", "Varun owes the scope.", 0.8, [j[5]], T0),
        FactChange("ADD", None, "interest", "VALORANT", "Plays VALORANT.", 0.8, [j[5]], T0, importance=5),
    ])
    a, b, quix, user, loop, val = [f for _ok, f in facts]
    decisions = [
        {"action": "merge", "fact_ids": [a, b], "keep_id": a, "text": "Swears casually in Hinglish with friends.",
         "importance": 5, "journal_ids": [], "reason": "the same trait twice"},
        {"action": "retire", "fact_ids": [quix], "keep_id": None, "text": "", "importance": None, "journal_ids": [],
         "reason": "stale since June 2025"},
        {"action": "retire", "fact_ids": [user], "keep_id": None, "text": "", "importance": None, "journal_ids": [],
         "reason": "x"},
        {"action": "retire", "fact_ids": [loop], "keep_id": None, "text": "", "importance": None, "journal_ids": [],
         "reason": "x"},
        {"action": "promote", "fact_ids": [val], "keep_id": None, "text": "", "importance": 7, "journal_ids": [],
         "reason": "plays nightly"},
    ]
    worker = PortraitWorker(store, client=ToolClient({"dream": [{"decisions": decisions}],
                                                      "save_portrait": [{"text": "Work", "profile": "",
                                                                         "personality": ""}]}),
                            log_dir=Path(tempfile.mkdtemp()))
    result = Dreamer(worker, max_changes=3).run(now=T0 + 3600)
    assert result.ok and result.applied == 3
    # a fact the user stated starts pinned (migration 33): dreaming leaves it alone
    assert any("pinned" in r for r in result.rejected) and any("open loops" in r for r in result.rejected)
    current = {f.id: f for f in store.portrait_facts()}
    merged = [f for f in current.values() if f.kind == "personality"]
    assert len(merged) == 1 and sorted(merged[0].source_ids) == sorted([j[0], j[1], j[2]])
    assert store.portrait_fact(b).superseded_by == merged[0].id
    assert quix not in current and current[val].importance == 7 and user in current and loop in current
    diary = store.changelog()
    assert {d["action"] for d in diary} == {"merge", "retire", "promote"} and all(d["text"] for d in diary)
    capped = Dreamer(worker, max_changes=1).validate(decisions, list(current.values()) + [store.portrait_fact(a)], set())
    assert any("cap" in r for r in capped[2]) or len(capped[0]) <= 1


# -- (7) persona ----------------------------------------------------------------------------------------


def test_persona_changes_are_quote_checked_capped_and_reach_the_prompt(store):
    from yuki.agent.prompt import profile_text
    from yuki.memory.api import MemoryClient
    from yuki.memory.persona import PersonaWriter
    from yuki.memory.portrait import PortraitWorker

    t1 = store.add_turn("s", 1, T0, "why did u use emdash?", "Sorry — fixed.", [], "final")
    t2 = store.add_turn("s", 2, T0 + 60, "lol yuh", "Cool.", [], "final")
    store.flush_turns()
    changes = [
        {"op": "add", "id": None, "text": "Plain punctuation, short lines; he calls out anything fancy.",
         "turn_ids": [1], "quote": "why did u use emdash?", "reason": "he called out an em dash"},
        {"op": "add", "id": None, "text": "Tease him more.", "turn_ids": [2], "quote": "tease me please",
         "reason": "made up"},
    ]
    worker = PortraitWorker(store, client=ToolClient({"update_persona": [{"changes": changes}]}),
                            log_dir=Path(tempfile.mkdtemp()))
    result = PersonaWriter(worker).run(now=T0 + 3600)
    assert result.ok and result.applied == 1 and "not the user's own words" in result.rejected[0]
    block = MemoryClient(store).yuki_persona()
    assert block == "- Plain punctuation, short lines; he calls out anything fancy."
    assert "How you talk with them" in profile_text(["S"], None, block)
    assert store.changelog(pass_="persona")[0]["text"].startswith("Yuki's voice, new:")


# -- (9) know-how from a correction -----------------------------------------------------------------------


def test_conversation_worker_saves_know_how_after_a_correction_led_to_success(store):
    from yuki.memory.conversations import ConversationWorker

    t1 = store.add_turn("s", 1, T0, "Reduce the YouTube volume", "Turning it down.", ["media_key: volume down"], "final")
    t2 = store.add_turn("s", 2, T0 + 40, "No. You reduced the system volume, not the YouTube volume.", "", [], "final")
    t3 = store.add_turn("s", 3, T0 + 50, "Reduce the YouTube video's volume by dragging the volume slider on the player",
                        "Turned the YouTube player volume down.",
                        ['click: on [88] Button "Mute (m)"', 'click: on [88] Button "Unmute"',
                         'click: clicked Slider "Volume" in Group "YouTube Video Player"'], "final")
    store.flush_turns()
    turns = store.turns_by_ids([t1, t2, t3])
    answer = {"operations": [], "journal_facts": [], "questions": [], "answers": [], "knowhow": [
        {"app": "chrome.exe", "text": "On YouTube's player, hover over the volume button so the slider appears, then "
                                      "drag it; clicking the button mutes the video.",
         "quote": "not the YouTube volume", "exchanges": [2, 3]},
        {"app": "chrome.exe", "text": "Made up.", "quote": "never said this", "exchanges": [3]},
    ]}
    worker = ConversationWorker(store, client=ToolClient({"record_conversation_memory": [answer]}),
                                embedder=StubEmbedder(), log_dir=Path(tempfile.mkdtemp()))
    result = worker.extract("s", turns, now=T0 + 120)
    assert result.ok
    [k] = store.knowhow_current("chrome.exe")
    assert "hover over the volume button" in k.text
    row = store._query("SELECT origin FROM knowhow WHERE id=?", (k.id,))[0][0]
    assert row == "conversation"
    # the next similar request gets it back by relevance (the same lookup every request makes)
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    client._embedder = StubEmbedder()
    assert any("hover over the volume button" in line for line in client.knowhow(app=None, query="a7 lower it"))


def test_the_prompt_asks_for_know_how_provenance_deep_recall_and_standing_requests():
    from yuki.agent.prompt import SYSTEM_PROMPT
    from yuki.agent.tools import MEMORY_TOOLS, TOOLS_BY_NAME

    for name in ("remember_how", "watch_for", "with_time_use", "where and when it came from"):
        assert name in SYSTEM_PROMPT
    # revamp D: recall is a kind of the one memory tool
    assert "watch_for" in MEMORY_TOOLS and "with_time_use" in TOOLS_BY_NAME["memory"]["input_schema"]["properties"]


# -- cost gates (the owner's ~$1/day target) ----------------------------------------------------------


def test_audit_runs_at_most_daily_unless_a_correction_was_folded(store):
    from yuki.memory.portrait import AUDIT_MIN_GAP_S, OpOutcome, PortraitScheduler, RunResult
    from yuki.memory.store import PortraitRunStats

    audits = []

    class W:
        def run(self, kind):
            r = RunResult(1, kind, True, stats=PortraitRunStats(kind=kind, model="stub"))
            if kind == "refresh":
                r.operations = [OpOutcome(op="UPDATE", fact_id=1, kind="work", subject="", text="x", confidence=1.0,
                                          journal_ids=[], based_on_facts=[9], reason="", status="applied")]
            return r

        def audit(self, *, full=False, reason=""):
            audits.append(reason)
            run = store.start_portrait_run("audit", "stub", at=now[0])
            store.finish_portrait_run(run, PortraitRunStats(kind="audit", model="stub"))

        def loop_check_pending(self):
            return False

    now = [T0]
    sched = PortraitScheduler(store, W(), db_path=store.path, idle_fn=lambda: 9999.0, clock=lambda: now[0])
    sched._run("nightly", "slot")
    now[0] += 3600
    sched._run("nightly", "again")                    # an hour later: no second audit
    assert len(audits) == 1
    sched._run("refresh", "the user corrected a fact")   # a correction was folded: audited at once
    assert len(audits) == 2 and "correction" in audits[-1]
    now[0] += AUDIT_MIN_GAP_S + 1
    sched._run("nightly", "next day")
    assert len(audits) == 3


def test_nightly_pass_renders_once_and_the_render_flag_waits_for_its_gap(store):
    from yuki.memory.portrait import PortraitScheduler, PortraitWorker
    from yuki.memory.store import RENDER_FLAG, flag_path

    jid = store.add_journal(T0, None, "Slack", "x", 5)
    store.commit_portrait_changes(None, [FactChange("ADD", None, "work", "w", "Works.", 0.8, [jid], T0)])
    client = ToolClient({"save_portrait": [{"text": "Work\n- works", "profile": "", "personality": ""}]})
    worker = PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp()))
    with worker.one_render():
        worker.render()
        worker.render()
    assert sum(r["tools"][0]["name"] == "save_portrait" for r in client.requests) == 1
    now = [store.latest_portrait().at + 60]
    sched = PortraitScheduler(store, worker, db_path=store.path, idle_fn=lambda: 0.0, clock=lambda: now[0],
                              render_debounce_s=0.0)
    sched._startup = False
    flag_path(store.path, RENDER_FLAG).write_text("x", encoding="utf-8")
    assert sched.tick() != "render" and flag_path(store.path, RENDER_FLAG).exists()   # rendered a minute ago
    now[0] += 31 * 60
    assert sched.tick() == "render"


def test_long_rules_never_push_a_preference_or_its_id_out_of_the_standing_block(store):
    from yuki.memory.api import MemoryClient
    long_rule = "Keep every reply short, plain, human and on point; no em dashes, no parentheses, no lists. " * 2
    for i in range(6):
        store.add_conversation_fact("rule", long_rule, quote=long_rule, subject=f"r{i}", at=T0 + i)
    store.add_conversation_fact("preference", "The user does not want nudges about focus.", quote="stfu",
                                subject="nudges", origin="model", at=T0 + 10)
    client = MemoryClient(store)
    pref = [f for f in store.conversation_facts(("preference",))][0]
    text = client.standing_context()
    assert f"[rule:{pref.id}]" in text                       # the id forget_rule takes is there
    assert client.revoke_rule(f"preference:{pref.id}") == pref.id
