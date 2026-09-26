"""Offline checks of the portrait audit, content dates and call-side questions (stub models, temp databases)."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.memory import embed as embed_module
from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, RECORD_FACTS_TOOL, JournalWorker, content_date
from yuki.memory.portrait import (
    AUDIT_PORTRAIT_TOOL,
    UPDATE_PORTRAIT_TOOL,
    PortraitScheduler,
    PortraitWorker,
    RunResult,
    _repair_tool_input,
)
from yuki.memory.store import AUDIT_FLAG, FactChange, PortraitRunStats, Store, flag_path

T0 = datetime(2026, 9, 25, 4, 0).timestamp()


class StubEmbedder:
    """Deterministic vectors from the words (so similar texts are near each other)."""

    model_name = "stub"

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(32, dtype=np.float32)
            for w in t.casefold().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 32] += 1.0
            out.append(v)
        return out


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(embed_module, "get_embedder", lambda: StubEmbedder())
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
    """``messages.create`` / ``messages.stream`` answering each tool with a queued input."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def _response(self, request):
        self.requests.append(request)
        name = request["tools"][0]["name"]
        queue = self.answers[name]
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use", usage=usage())

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


def fact(text, source, nature="fact", importance=5, content=""):
    return {"text": text, "nature": nature, "importance": importance, "source": source, "content_date": content}


# -- content dates -----------------------------------------------------------------------------


def test_content_date_parsing():
    seen = datetime(2026, 9, 24, 18, 9).timestamp()
    assert datetime.fromtimestamp(content_date("2025-06-12", seen)) == datetime(2025, 6, 12, 12, 0)
    assert datetime.fromtimestamp(content_date("2025-06", seen)) == datetime(2025, 6, 15, 12, 0)
    assert content_date("2026-09-24", seen) is None     # the day it was seen: the source dates it
    assert content_date("2027-01-01", seen) is None and content_date("", seen) is None
    assert content_date("June 2025", seen) is None and content_date("1850-01-01", seen) is None
    assert "content_date" in RECORD_FACTS_TOOL["input_schema"]["properties"]["facts"]["items"]["required"]
    assert "content_date" not in RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["facts"]["items"]["properties"]


def test_page_delta_gets_its_top_and_old_content_is_dated_by_it(store):
    tid = store.upsert_thread("Google Chrome", "YOU WERE THE ONE - YouTube", "https://www.youtube.com/watch?v=1",
                              scope="https://www.youtube.com/watch?v=1", kind="video")
    top = "Video: YOU WERE THE ONE (Music Video)\nBy: Driftwood Music\n738 views Jun 12, 2025"
    store.add_capture(tid, T0, "title", top, top, kind="video")
    with tempfile.TemporaryDirectory() as logs:
        JournalWorker(store, client=ToolClient({"record_facts": [{"facts": [], "questions": []}]}),
                      embedder=StubEmbedder(), log_dir=Path(logs)).run_once()
        store.add_capture(tid, T0 + 60, "focus", top + "\nquix is on break he got exams", "quix is on break he got exams",
                          kind="video")
        answer = {"facts": [
            fact("The user watched 'YOU WERE THE ONE' by Driftwood Music on YouTube.", 1, content=""),
            fact("In the description of 'YOU WERE THE ONE', posted around June 2025, the uploader said quix was on "
                 "a break for exams.", 1, content="2025-06-12"),
        ], "questions": []}
        client = ToolClient({"record_facts": [answer]})
        [result] = JournalWorker(store, client=client, embedder=StubEmbedder(), log_dir=Path(logs)).run_once()
    assert result.ok
    request = client.requests[0]["messages"][0]["content"]
    assert "TOP OF THIS PAGE" in request and "Jun 12, 2025" in request
    from yuki.memory.journal import system_text

    sent = system_text(client.requests[0]["system"])
    assert "content_date" in sent or "content date" in sent.lower()
    rows = {j.fact[:20]: j for j in store.journal_between()}
    watched = next(j for j in rows.values() if j.fact.startswith("The user watched"))
    said = next(j for j in rows.values() if "quix" in j.fact)
    assert abs(watched.at - (T0 + 60)) < 1
    assert datetime.fromtimestamp(said.at).date() == datetime(2025, 6, 12).date()


# -- a call's own side asks about names the user said -------------------------------------------


def test_call_side_questions_are_stored_with_their_evidence(store):
    utterances = [{"at": T0 + 10, "end": T0 + 14, "text": "main aur Tibo kal khelenge", "lang": "hi"}]
    store.add_call_side(7, app="Discord", process="discord", started_at=T0, ended_at=T0 + 60,
                        utterances=utterances, final=True)
    answer = {"facts": [fact("In a Discord call, the user said they and Tibo will play tomorrow.", 1)],
              "commitments": [],
              "questions": [{"subject": "Tibo", "kind": "person", "question": "Who is Tibo to the user?",
                             "evidence": "named in a Discord call", "why": "a person the user plays with",
                             "importance": 5, "source": 1}]}
    with tempfile.TemporaryDirectory() as logs:
        client = ToolClient({"record_call_side": [answer]})
        [result] = JournalWorker(store, client=client, embedder=StubEmbedder(), log_dir=Path(logs)).run_once()
    assert result.ok and result.questions and result.questions[0][1] == "added"
    [q] = store.questions()
    [j] = store.journal_between()
    assert q.subject == "Tibo" and q.origin == "journal" and q.source_ids == [j.id]
    from yuki.memory.journal import system_text

    assert "ASK ABOUT NAMES" in system_text(client.requests[0]["system"])
    assert RECORD_CALL_SIDE_TOOL["input_schema"]["required"] == ["facts", "commitments", "questions"]


# -- the audit ------------------------------------------------------------------------------------


def journal_page(store, logs, app, title, text, answer_facts, at):
    tid = store.upsert_thread(app, title, None, scope=title, kind="page")
    store.add_capture(tid, at, "title", text, text, kind="page")
    JournalWorker(store, client=ToolClient({"record_facts": [{"facts": answer_facts, "questions": []}]}),
                  embedder=StubEmbedder(), log_dir=Path(logs)).run_once()
    return [j for j in store.journal_between() if j.thread_id == tid]


def audit_setup(store, logs):
    [portal] = journal_page(store, logs, "Claude", "LVU academic portal",
                            "Welcome ElinaMathur 21BXYZ042 | BDES | Bachelor of Design | Current Term 5",
                            [fact("The user is enrolled in a Bachelor of Design program at LVU, term 5.", 1)], T0)
    [helped] = journal_page(store, logs, "Discord", "call notes",
                            "helped elina set up claude code and her space med roadmap",
                            [fact("The user helped Elina set up Claude Code and test a space science roadmap.", 1)],
                            T0 + 3600)
    [grade] = journal_page(store, logs, "WhatsApp", "Bro", "5th sem certificate BA Media",
                           [fact("The user sent Bro a 5th semester certificate for a BA Media program.", 1)], T0 - 86400)
    changes = [
        FactChange("ADD", None, "work", "Design program", "The user is in term 5 of a design degree.",
                   0.8, [portal.id], T0),
        FactChange("ADD", None, "person", "Elina", "Elina is someone the user helped on a Discord call.", 0.4,
                   [helped.id], T0 + 3600),
        FactChange("ADD", None, "interest", "CSE", "The user studied BA Media.", 0.6, [grade.id], T0 - 86400),
    ]
    ids = [new for _ok, new in store.commit_portrait_changes(None, changes)]
    return {"portal": portal.id, "helped": helped.id, "grade": grade.id, "course": ids[0], "elina": ids[1],
            "cse": ids[2]}


def audit_op(**kw):
    base = {"op": "UPDATE", "fact_id": None, "kind": "person", "subject": "", "text": "", "confidence": 0.6,
            "journal_ids": [], "episode_ids": [], "based_on_facts": [], "reason": "", "loop": None, "aspect": None,
            "finding": 1, "relationship": None}
    return {**base, **kw}


def test_audit_resolves_a_misattribution_asks_and_records_why(store):
    with tempfile.TemporaryDirectory() as logs:
        ids = audit_setup(store, logs)
        findings = [{"finding": "misattribution", "fact_ids": [ids["course"], ids["elina"]],
                     "journal_ids": [ids["portal"], ids["helped"]], "decision": "resolve",
                     "reasoning": "The portal greets ElinaMathur; the user helped Elina that night."}]
        operations = [
            audit_op(fact_id=ids["elina"], subject="Elina",
                     text="Elina, whom the user helped on a Discord call, is in term 5 of a design degree.",
                     journal_ids=[ids["portal"], ids["helped"]]),
            audit_op(op="INVALIDATE", fact_id=ids["course"], kind="work", journal_ids=[ids["portal"]]),
            audit_op(fact_id=ids["cse"], kind="interest", subject="CSE", text="The user's friend studied CSE.",
                     journal_ids=[ids["grade"]], relationship={"words": "friend", "journal_id": ids["grade"]},
                     finding=2),
        ]
        answer = {
            # as Sonnet sent it on 2026-09-25 (no strict on Bedrock): the operations inside the findings string
            "findings": json.dumps(findings) + ', "operations": ' + json.dumps(operations),
            "questions": [{"subject": "Elina", "kind": "person", "question": "Who is Elina to the user?",
                           "evidence": "the user helped her on a Discord call", "why": "no entry says",
                           "importance": 6, "journal_ids": [ids["helped"]], "fact_ids": [ids["elina"]], "finding": 1}],
        }
        client = ToolClient({"audit_portrait": [answer], "save_portrait": [{"text": "Work\n- ...", "personality": ""}]})
        worker = PortraitWorker(store, client=client, log_dir=Path(logs))
        result = worker.audit(reason="test")
    assert result is not None and result.ok, result.error if result else None
    request = client.requests[0]["messages"][0]["content"]
    assert "UNDER REVIEW" in request and "SOURCE EXCERPTS" in request and "Welcome ElinaMathur" in request
    assert 'in page "LVU academic portal"' in request       # where each entry comes from
    assert store.portrait_fact(ids["course"]).valid_to is not None
    elina = store.portrait_fact(ids["elina"])
    assert elina.valid_to is not None and elina.superseded_by
    assert "design" in store.portrait_fact(elina.superseded_by).text
    assert store.portrait_fact(ids["cse"]).valid_to is None      # "friend" is not in the cited entry: refused
    statuses = [o.status for o in result.operations]
    assert any("relationship it states is not in the words" in s for s in statuses)
    [q] = store.questions()
    assert q.subject == "Elina" and q.origin == "portrait" and q.fact_ids == [ids["elina"]]
    [finding] = store.audit_findings()
    assert finding["finding"] == "misattribution" and finding["decision"] == "resolved"
    assert finding["new_fact_ids"] == [elina.superseded_by] and finding["question_ids"] == [q.id]
    assert "ElinaMathur" in finding["reasoning"]
    assert store.portrait_runs(1)[0]["kind"] == "audit" and store.latest_portrait() is not None
    assert worker.audit_checkpoint() is not None


def test_audit_scope_only_what_new_evidence_touches(store):
    with tempfile.TemporaryDirectory() as logs:
        ids = audit_setup(store, logs)
        client = ToolClient({"audit_portrait": [{"findings": [], "operations": [], "questions": []}]})
        worker = PortraitWorker(store, client=client, log_dir=Path(logs))
        under, reasons, _recent = worker.audit_scope(T0 + 7200)
        assert {f.id for f in under} == {ids["course"], ids["elina"], ids["cse"]} and "first audit" in reasons.values()
        assert worker.audit(now=T0 + 7200) is not None and len(client.requests) == 1
        # nothing new: no model call
        assert worker.audit(now=T0 + 7300) is None and len(client.requests) == 1
        # new evidence close in meaning to Elina's fact (the shared embedder's vectors): only hers is looked at
        def axis(i):
            v = np.zeros(32, dtype=np.float32)
            v[i] = 1.0
            return v

        store.add_portrait_fact_vectors([(ids["course"], axis(0)), (ids["elina"], axis(1)), (ids["cse"], axis(2))],
                                        "stub")
        [thanks] = journal_page(store, logs, "WhatsApp", "RES", "elina: thanks for the help",
                                [fact("In the WhatsApp group RES, Elina thanked the user for the help.", 1)], T0 + 9000)
        store.add_vectors([(thanks.id, axis(1) + 0.1 * axis(5))], "stub")
        store._conn.commit()
        under, reasons, recent = worker.audit_scope(T0 + 9100)
        assert [f.id for f in under] == [ids["elina"]] and "close to it in meaning" in reasons[ids["elina"]]
        assert len(recent) == 1
        # a new version that only rewords (same meaning) is not new evidence; a moved one is
        [(ok, reworded)] = store.commit_portrait_changes(None, [FactChange("UPDATE", ids["cse"], "interest", "CSE",
                                                                           "The user studied BE in CSE.", 0.6,
                                                                           [thanks.id], T0 + 9200)])
        store.add_portrait_fact_vectors([(reworded, axis(2) + 0.05 * axis(6))], "stub")
        store._conn.commit()
        under, reasons, _ = worker.audit_scope(T0 + 9300)
        assert reworded not in {f.id for f in under}


def test_repair_of_mangled_tool_input():
    mangled = {"findings": '[{"finding": "stale"}], "operations": [{"op": "UPDATE"}]', "questions": []}
    fixed = _repair_tool_input(mangled, ("findings", "operations", "questions"))
    assert fixed["findings"] == [{"finding": "stale"}] and fixed["operations"] == [{"op": "UPDATE"}]
    assert _repair_tool_input({"findings": [], "operations": "nonsense"}, ("findings", "operations")) == \
        {"findings": [], "operations": "nonsense"}


def test_schemas_require_questions_and_relationships():
    assert UPDATE_PORTRAIT_TOOL["input_schema"]["required"] == ["operations", "questions"]
    op_item = AUDIT_PORTRAIT_TOOL["input_schema"]["properties"]["operations"]["items"]
    assert {"finding", "relationship"} <= set(op_item["required"])
    assert AUDIT_PORTRAIT_TOOL["input_schema"]["required"] == ["findings", "operations", "questions"]


# -- scheduling -------------------------------------------------------------------------------------


class FakeWorker:
    def __init__(self):
        self.audits = []
        self.runs = []

    def run(self, kind):
        self.runs.append(kind)
        return RunResult(1, kind, True, stats=PortraitRunStats(kind=kind, model="stub"))

    def rerender(self, reason=""):
        return RunResult(1, "render", True, stats=PortraitRunStats(kind="render", model="stub"))

    def audit(self, *, full=False, reason=""):
        self.audits.append((full, reason))
        return None

    def loop_check_pending(self):
        return False


def test_scheduler_audits_after_journal_runs_and_on_the_flag(store):
    store.save_portrait("Work\n- x")
    worker = FakeWorker()
    sched = PortraitScheduler(store, worker, db_path=store.path, idle_fn=lambda: 9999.0, clock=lambda: T0)
    assert sched.tick() == "weekly"                       # the missed weekly slot, run at start
    assert worker.audits == [(True, "after the weekly run")]
    flag_path(store.path, AUDIT_FLAG).write_text("all", encoding="utf-8")
    assert sched.tick() == "audit" and worker.audits[-1] == (True, "audit flag")
    assert not flag_path(store.path, AUDIT_FLAG).exists()


def test_episodes_since_the_last_journal_run_not_the_last_loop_check(store):
    nightly = store.start_portrait_run("nightly", "stub", at=T0 - 20 * 3600)
    store.finish_portrait_run(nightly, PortraitRunStats(kind="nightly", model="stub"))
    loops = store.start_portrait_run("loops", "stub", at=T0 - 600)
    store.finish_portrait_run(loops, PortraitRunStats(kind="loops", model="stub"))
    worker = PortraitWorker(store, client=object())
    assert worker._episode_since("nightly", T0) == pytest.approx(T0 - 20 * 3600)


def test_migration_15_audit_table(store):
    assert store._query("PRAGMA user_version")[0][0] >= 15
    cols = {r["name"] for r in store._query("PRAGMA table_info(portrait_audits)")}
    assert {"finding", "decision", "fact_ids", "new_fact_ids", "question_ids", "reasoning_ciphertext"} <= cols
