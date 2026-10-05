"""Offline checks of natures, the Personality section and the re-journal (stub models, temp databases)."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, RECORD_FACTS_TOOL, JournalWorker, system_prompt
from yuki.memory.personality import PERSONALITY_HEADING, nature_mark, split_personality, usable
from yuki.memory.portrait import PortraitWorker
from yuki.memory.store import REFRESH_FLAG, FactChange, JournalEntry, NewFact, Store, flag_path

T0 = datetime(2026, 9, 25, 1, 0).timestamp()


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


def usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0)


#: The admission judge (yuki.memory.admission) admitting every call fact, so these checks see them in the journal.
ADMIT_ALL = {"verdicts": [{"fact": n, "verdict": "admit", "sensitive": "none", "shown": ""} for n in range(1, 41)]}


class ToolClient:
    """``messages.create`` / ``messages.stream`` answering each tool with a queued input."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = answers
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def _response(self, request):
        self.requests.append(request)
        name = request["tools"][0]["name"]
        data = self.answers[name].pop(0) if len(self.answers[name]) > 1 else self.answers[name][0]
        block = SimpleNamespace(type="tool_use", name=name, input=data)
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage())

    def create(self, **request):
        return self._response(request)

    def stream(self, **request):
        response = self._response(request)

        class _Stream:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def get_final_message(self_inner):
                return response

        return _Stream()


# -- the store: migration 14 ----------------------------------------------------------


def test_migration_14_nature_and_supersede(store):
    assert store._query("PRAGMA user_version")[0][0] >= 14   # 15: the portrait audit
    cols = {r["name"] for r in store._query("PRAGMA table_info(journal)")}
    assert {"nature", "superseded_at", "superseded_by"} <= cols
    old = store.add_journal(T0, None, "Discord", "The user needs a pilot who earns millions.", 4)
    store._query("UPDATE journal SET nature=NULL WHERE id=?", (old,))            # a row from before natures
    assert store.journal_between()[0].nature == "fact"
    from yuki.memory.store import ModelCall

    call = ModelCall(at=T0, thread_id=None, model="stub")
    first = store.commit_batch(call, [], [NewFact(T0, None, "Discord", None, "X said Y.", 4, nature="fact")])
    second = store.commit_batch(call, [], [NewFact(T0, None, "Discord", None, "The user joked Z.", 4,
                                                   nature="humor")], supersedes=[first])
    live = store.journal_between()
    assert [j.fact for j in live] == ["The user needs a pilot who earns millions.", "The user joked Z."]
    assert live[-1].nature == "humor"
    history = store.journal_of_batches([first], include_superseded=True)
    assert history[0].fact == "X said Y." and store.superseded_journal([history[0].id]) == {history[0].id: second}
    assert json.loads(store._query("SELECT supersedes FROM journal_batches WHERE id=?", (second,))[0][0]) == [first]


def test_readers_label_and_filter():
    j = JournalEntry(1, T0, None, "Discord", None, "x", 4, None, nature="uncertain")
    assert not usable(j) and "not a fact" in nature_mark("uncertain") and "humour" in nature_mark("humor")
    assert nature_mark("fact") == "" and nature_mark(None) == ""


# -- the journal: natures in the prompts and tools ------------------------------------------


def test_prompts_keep_jokes_as_humour():
    call = system_prompt(["Kian"], questions=False, call_side=True)
    assert "WHAT EACH ITEM IS" in call and "leave it out rather than" not in call
    assert "record the bit as a bit" in call and "never join words from different utterances" in call
    for tool in (RECORD_FACTS_TOOL, RECORD_CALL_SIDE_TOOL):
        item = tool["input_schema"]["properties"]["facts"]["items"]
        assert "nature" in item["required"] and item["properties"]["nature"]["enum"][3] == "humor"
    assert "uncertain" not in RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["facts"]["items"]["properties"]


def call_capture(store: Store, call_id: int = 4) -> int:
    utterances = [
        {"at": T0 + 10, "end": T0 + 14, "text": "bhai mujhe ek pilot chahiye jo bahut paisa kamaye", "lang": "hi"},
        {"at": T0 + 20, "end": T0 + 24, "text": "you guys are my newest friends since I moved to Eastvale",
         "lang": "en"},
        {"at": T0 + 30, "end": T0 + 34, "text": "I'll teach you number systems tomorrow afternoon", "lang": "en"},
    ]
    return store.add_call_side(call_id, app="Discord", process="discord", started_at=T0, ended_at=T0 + 60,
                               utterances=utterances, final=True)


CALL_ANSWER_OLD = {"facts": [
    {"text": "The user said they need a pilot who earns millions so they do not have to work.",
     "nature": "fact", "importance": 4, "source": 1},
    {"text": "The user has known the call participants for about 2 years.", "nature": "fact", "importance": 5,
     "source": 2}],
    "commitments": [{"text": "Teach number systems on 2026-09-26 afternoon.", "quote": "I'll teach you number systems",
                     "due": "2026-09-26", "source": 3, "subject": "maths"}]}
CALL_ANSWER_NEW = {"facts": [
    {"text": "In a Discord call, the user joked that they need a pilot partner earning a million a year.",
     "nature": "humor", "importance": 4, "source": 1},
    {"text": "In a Discord call, the user called the people on the call their newest friends since they moved to "
             "Eastvale.", "nature": "fact", "importance": 5, "source": 2},
    {"text": "The user switches between Hindi and English mid-sentence with friends.", "nature": "trait",
     "importance": 5, "source": 2}],
    "commitments": [{"text": "Teach number systems on 2026-09-26 afternoon.", "quote": "I'll teach you number systems",
                     "due": "2026-09-26", "source": 3, "subject": "maths"}]}


def test_call_journal_natures_and_rejournal_supersedes(store):
    from yuki.memory import rejournal

    with tempfile.TemporaryDirectory() as logs:
        call_capture(store)
        worker = JournalWorker(store, client=ToolClient({"record_call_side": [CALL_ANSWER_OLD], "admit_call_facts": [ADMIT_ALL]}),
                               embedder=StubEmbedder(), log_dir=Path(logs))
        [first] = worker.run_once()
        assert first.ok and first.natures == ["fact", "fact"]
        assert len([f for f in store.conversation_facts(("commitment",)) if f.origin == "call"]) == 1
        # re-journal with the fixed pipeline (a stub standing in for the model's new reading)
        client = ToolClient({"record_call_side": [CALL_ANSWER_NEW], "admit_call_facts": [ADMIT_ALL]})
        lines: list[str] = []
        summary = rejournal.run(store, thread_kind="my_call_side", since=T0 - 3600, until=None, portrait="flag",
                                log_dir=Path(logs), client=client, out=lines.append)
        assert summary["facts_before"] == 2 and summary["facts_after"] == 3 and not summary["failed"]
        live = store.journal_between()
        assert [j.nature for j in live] == ["humor", "fact", "trait"]
        assert all("2 years" not in j.fact for j in live)
        # the call's to-do stays on its path, not written twice
        assert len([f for f in store.conversation_facts(("commitment",)) if f.origin == "call"]) == 1
        # what the model was shown as already recorded did not include the facts being replaced
        request = client.requests[0]["messages"][0]["content"]
        assert "millions so they do not have to work" not in request
        assert flag_path(store.path, REFRESH_FLAG).exists()
        assert any("BEFORE:" in x for x in lines) and any("[humor]" in x for x in lines)
        # a second dry run lists the new facts as the ones in place
        dry: list[str] = []
        rejournal.run(store, thread_kind="my_call_side", since=T0 - 3600, until=None, dry_run=True, out=dry.append)
        assert any("[trait]" in x for x in dry) and not any("millions so they" in x for x in dry)


def test_rejournal_that_finds_nothing_keeps_the_earlier_facts(store):
    from yuki.memory import rejournal

    with tempfile.TemporaryDirectory() as logs:
        call_capture(store)
        JournalWorker(store, client=ToolClient({"record_call_side": [CALL_ANSWER_OLD], "admit_call_facts": [ADMIT_ALL]}),
                      embedder=StubEmbedder(), log_dir=Path(logs)).run_once()
        empty = ToolClient({"record_call_side": [{"facts": [], "commitments": []}]})
        lines: list[str] = []
        summary = rejournal.run(store, thread_kind="my_call_side", since=T0 - 3600, until=None, portrait="skip",
                                log_dir=Path(logs), client=empty, out=lines.append)
        assert summary.get("kept") == 1 and len(empty.requests) == 2          # read twice, then kept
        assert [j.nature for j in store.journal_between()] == ["fact", "fact"]
        assert any("kept as they are" in x for x in lines)


# -- the portrait: personality facts, what may support what, the section ------------------


def portrait_setup(store: Store) -> dict[str, int]:
    ids = {}
    for key, text, nature in (
        ("joke", "In a Discord call, the user joked that they need a pilot partner earning a million a year.",
         "humor"),
        ("opus", "The user said, possibly as banter, that they have unlimited Claude Opus accounts from Anthropic.",
         "uncertain"),
        ("talk", "The user swears casually and mixes Hindi and English with friends.", "trait"),
        ("story", "The user said that in college their alarm clock was so loud the neighbours complained.",
         "story"),
        ("fact", "The user works as a developer building agent pipelines.", "fact"),
    ):
        ids[key] = store.add_journal(T0, None, "Discord", text, 5)
        store._query("UPDATE journal SET nature=? WHERE id=?", (nature, ids[key]))
    return ids


def op(**kw):
    base = {"op": "ADD", "fact_id": None, "kind": "personality", "subject": "", "text": "", "confidence": 0.4,
            "journal_ids": [], "episode_ids": [], "based_on_facts": [], "reason": "", "loop": None, "aspect": None}
    return {**base, **kw}


def test_portrait_validation_by_nature(store):
    ids = portrait_setup(store)
    worker = PortraitWorker(store, client=object())
    journal = store.journal_between()
    items = [
        op(kind="work", subject="money", text="The user will marry a rich pilot.", journal_ids=[ids["joke"]]),
        op(kind="preference", subject="Claude", text="The user has unlimited Opus accounts.", journal_ids=[ids["opus"]]),
        op(subject="rich partner bit", text="Jokes about needing a millionaire pilot partner (Discord, 2026-09-25).",
           journal_ids=[ids["joke"]], aspect="joke"),
        op(subject="register", text="Swears casually, Hinglish with friends.", journal_ids=[ids["talk"]]),
        op(subject="alarm clock", text="Tells how the neighbours complained about his alarm clock in college.", journal_ids=[ids["story"]],
           aspect="story"),
        op(kind="work", subject="developer", text="Developer building agent pipelines.", journal_ids=[ids["fact"]]),
    ]
    changes, outcomes = worker._validate(items, [], journal, T0 + 60)
    status = [o.status for o in outcomes]
    assert status[0].startswith("rejected: rests only on humor")
    assert status[1].startswith("rejected: rests only on uncertain")
    assert status[2] == "applied" and changes[0].aspect == "joke"
    assert status[3] == "rejected: a personality fact needs its aspect"
    assert status[4] == "applied" and status[5] == "applied"


def test_rejournaled_fact_may_be_dropped_and_is_marked(store):
    ids = portrait_setup(store)
    [(ok, fid)] = store.commit_portrait_changes(None, [FactChange("ADD", None, "work", "Claude", "Has unlimited Opus.",
                                                                  0.5, [ids["opus"]], T0)])
    store._query("UPDATE journal SET superseded_at=?, superseded_by=99 WHERE id=?", (T0, ids["opus"]))
    worker = PortraitWorker(store, client=object())
    facts = store.portrait_facts()
    line = worker._fact_line(facts[0], lambda v, c: str(v or ""), store.superseded_journal([ids["opus"]]))
    assert "re-journaled entries" in line
    changes, outcomes = worker._validate([op(op="INVALIDATE", fact_id=fid, kind="work")], facts, [], T0 + 60)
    assert outcomes[0].status == "applied" and changes[0].op == "INVALIDATE"


def test_render_places_personality_under_its_heading(store):
    ids = portrait_setup(store)
    store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "developer", "Developer building agent pipelines.", 0.8, [ids["fact"]], T0),
        FactChange("ADD", None, "personality", "rich partner bit", "Jokes about needing a millionaire pilot partner.",
                   0.4, [ids["joke"]], T0, aspect="joke"),
    ])
    client = ToolClient({"save_portrait": [{"text": "Work\nThe user is a developer.",
                                            "personality": "running jokes and bits:\n- jokes about a millionaire partner"}]})
    worker = PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp()))
    text = worker.render()
    rest, section = split_personality(text)
    assert section.startswith(PERSONALITY_HEADING) and "millionaire partner" in section
    assert rest.startswith("Work") and "Open loops" in rest and "millionaire" not in rest
    sent = client.requests[0]["messages"][0]["content"]
    assert "personality (for the personality field only):" in sent and "running jokes and bits (humour" in sent


# -- readers downstream ----------------------------------------------------------------------


def test_coach_leaves_out_uncertain_and_labels_jokes(store):
    from yuki.memory.nudges import NudgeConfig, Trigger, build_coach_message, observe

    portrait_setup(store)
    view = observe(store, T0 + 600, NudgeConfig())
    view.since = T0 - 60
    text = build_coach_message(store, view, Trigger("periodic"), [], NudgeConfig())
    assert "unlimited Claude Opus" not in text and "[joke/banter - humour, not a fact]" in text


def test_recall_and_mouth_carry_it():
    from yuki.agent.memory import format_recall
    from yuki.voice.mouth import MouthMemory, PORTRAIT_CHARS, fetch_memory

    out = format_recall([{"kind": "fact", "at": "2026-09-25T01:43", "fact": "The user joked X.", "nature": "humor"}],
                        query="x")
    assert "humour, not a fact" in out

    page = "Work\n" + ("The user builds things. " * 200) + f"\n\n{PERSONALITY_HEADING}\nhow they talk:\n- Hinglish" \
           "\n\nOpen loops: none on record."

    class Memory:
        def context(self, **_):
            return SimpleNamespace(portrait=page, todos=[], standing=None, resume=None, source="memory", error=None,
                                   elapsed_ms=1.0)

    got = fetch_memory(Memory())
    assert "- Hinglish" in got.portrait and len(got.portrait) < PORTRAIT_CHARS + 400
    assert isinstance(got, MouthMemory)
