"""Revamp A6 (the design notes): the portrait's fields within their budgets, the card
writers' ABOUT THE USER per kind, the to-do list kept true (mirrors, the check on newer evidence, jokes never a to-do,
dated plans from calls) and the follow-up scan's new sources and vet.

Temp databases and stub models only; nothing touches the desktop or a real model.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.store import FactChange, Store

T0 = datetime(2026, 10, 1, 21, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def usage():
    return SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class ToolClient:
    """Answers each forced tool call from a queue per tool name (the last answer repeats), keeping the requests."""

    def __init__(self, answers: dict[str, list[Any]]) -> None:
        self.answers = answers
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        name = request["tools"][0]["name"]
        queue = self.answers[name]
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use", usage=usage())

    def stream(self, **request: Any) -> Any:
        response = self.create(**request)

        class _Stream:
            def __enter__(s):
                return s

            def __exit__(s, *exc):
                return False

            def get_final_message(s):
                return response

        return _Stream()


# -- P0.2: the portrait's fields within their budgets ---------------------------------------------------------


def _facts(store: Store) -> None:
    jid = store.add_journal(T0, None, "Discord", "x", 5)
    store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "Corvana", "Works at Corvana.", 0.9, [jid], T0),
        FactChange("ADD", None, "interest", "music", "Produces music as Mafex.", 0.8, [jid], T0),
        FactChange("ADD", None, "routine", "sleep", "Goes to bed around 4-5 am.", 0.8, [jid], T0),
        FactChange("ADD", None, "person", "Hemanth", "Hemanth is his best friend.", 0.9, [jid], T0),
        FactChange("ADD", None, "open_loop", "Varun", "Varun owes him the scope (asked in Slack on 2026-09-30, "
                   "said he would send it).", 0.8, [jid], T0),
    ])


def _fields(**over: str) -> dict[str, str]:
    base = {"work": "He works at Corvana.", "interests": "He produces music as Mafex.",
            "routines": "He goes to bed around 4-5 am.", "behaviour": "", "people": "Hemanth is his best friend.",
            "preferences": "", "relationship": "", "personality": ""}
    return {**base, **over}


def test_render_asks_once_to_shorten_an_over_budget_field_and_never_cuts(store):
    from yuki.memory.portrait import FIELD_BUDGETS, PortraitWorker

    _facts(store)
    long_work = " ".join(f"He shipped release number {i} of the platform." for i in range(80))   # ~3,500 chars
    short_work = "He works at Corvana, shipping the demo platform's releases."
    client = ToolClient({"save_portrait": [_fields(work=long_work)], "shorten_portrait": [{"work": short_work}]})
    log_dir = Path(tempfile.mkdtemp())
    text = PortraitWorker(store, client=client, log_dir=log_dir).render()
    assert short_work in text and "release number 79" not in text
    assert [r["tools"][0]["name"] for r in client.requests] == ["save_portrait", "shorten_portrait"]
    shorten = client.requests[1]["messages"][0]["content"]
    assert f"{len(long_work)} characters, budget {FIELD_BUDGETS['work']}" in shorten
    events = [json.loads(line)["type"] for f in log_dir.glob("portrait-*.jsonl") for line in f.read_text().splitlines()]
    assert "portrait_over_budget" in events and "portrait_truncated" not in events
    page = store.latest_portrait()
    assert page.sections["work"] == short_work and page.sections["interests"].startswith("He produces")
    assert "Routines\nHe goes to bed" in text and "People\nHemanth" in text and "Interests\n" in text
    # one open loop, one line: its evidence stays in the record
    loop_line = next(line for line in text.splitlines() if line.startswith("- Varun"))
    assert "Varun owes him the scope." in loop_line and "asked in Slack" not in loop_line


def test_a_field_still_over_after_the_reask_is_cut_at_a_whole_sentence(store):
    from yuki.memory.portrait import FIELD_BUDGETS, PortraitWorker

    _facts(store)
    long_people = " ".join(f"Friend {i} talks with him on Discord about games." for i in range(60))
    client = ToolClient({"save_portrait": [_fields(people=long_people)], "shorten_portrait": [{"people": long_people}]})
    PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp())).render()
    people = store.latest_portrait().sections["people"]
    assert len(people) <= FIELD_BUDGETS["people"] and people.endswith("games.")


def test_check_loops_keeps_the_fields(store):
    from yuki.memory.portrait import PortraitWorker

    _facts(store)
    worker = PortraitWorker(store, client=ToolClient({"save_portrait": [_fields()]}), log_dir=Path(tempfile.mkdtemp()))
    worker.render()
    worker._splice_loops(None, time.time())
    assert store.latest_portrait().sections["work"] == "He works at Corvana."


# -- P0.3: ABOUT THE USER per kind ---------------------------------------------------------------------------


def test_about_lines_give_every_kind_its_own_room_newest_first(store):
    from yuki.memory.nudges import COACH_ABOUT_KINDS, PRESENCE_ABOUT_KINDS, _about_lines

    jid = store.add_journal(T0, None, "Discord", "x", 5)
    changes = [FactChange("ADD", None, "work", f"w{i}", f"Old work fact number {i} " + "x" * 200, 0.8, [jid],
                          T0 - 86400 * (30 - i)) for i in range(20)]
    changes += [FactChange("ADD", None, "routine", "sleep", "Goes to bed at 4-5 am.", 0.8, [jid], T0),
                FactChange("ADD", None, "person", "Hemanth", "Best friend; Discord DM and WhatsApp.", 0.9, [jid], T0),
                FactChange("ADD", None, "preference", "browser", "Uses Zen for Meet, Chrome for YouTube.", 0.8, [jid],
                           T0),
                FactChange("ADD", None, "behaviour", "valorant", "Plays VALORANT 21:00-02:00 most nights.", 0.8, [jid],
                           T0)]
    store.commit_portrait_changes(None, changes)
    plain = lambda v, cap: str(v)[:cap]   # noqa: E731
    lines = _about_lines(store, COACH_ABOUT_KINDS, plain)
    text = "\n".join(lines)
    for needle in ("Goes to bed", "person - Hemanth", "Uses Zen", "Plays VALORANT"):
        assert needle in text
    work = [line for line in lines if line.startswith("- work")]
    assert work and "number 19" in work[0]               # the newest work fact first, not the 24 Sept ones
    assert "person" in PRESENCE_ABOUT_KINDS and "preference" in PRESENCE_ABOUT_KINDS


def test_about_lines_leave_out_a_fact_whose_evidence_was_withdrawn(store):
    from yuki.memory.nudges import _about_lines

    sample = store.add_journal(T0, None, "Chrome", "The user said they got a job offer from Google.", 7)
    store._write(lambda conn: conn.execute("UPDATE journal SET tainted=1 WHERE id=?", (sample,)))
    real = store.add_journal(T0, None, "Discord", "Works at Corvana.", 7)
    store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "offer", "Got a job offer from Google.", 0.6, [sample], T0),
        FactChange("ADD", None, "work", "job", "Works at Corvana.", 0.9, [real], T0)])
    text = "\n".join(_about_lines(store, ("work",), lambda v, cap: str(v)))
    assert "Corvana" in text and "Google" not in text


# -- P0.5: the to-do list kept true --------------------------------------------------------------------------


class _Worker:
    """What the commitment check needs of the conversation worker: its store, model call, log and seal."""

    def __init__(self, store: Store, answer: dict[str, Any]) -> None:
        self.store = store
        self.answer = answer
        self.calls: list[str] = []
        self.logged: list[tuple[str, dict]] = []

    def _call(self, purpose, system, user, tool, call, model=None):
        self.calls.append(user)
        call.cost_usd = 0.001
        return self.answer, SimpleNamespace(content=[]), {}

    def _log(self, type, **fields):
        self.logged.append((type, fields))

    def _seal(self, value):
        return "sealed"


def test_a_done_job_closes_its_mirror_with_a_true_note(store):
    """09 §3.5: Yuki tagged Hemanth (its record #37 done) while the user's mirror #38 stayed open for two days."""
    from yuki.memory.commitments import close_mirrors

    tid = store.add_turn("s", 1, T0, "tag Hemanth and ask when he joins", "On it.", [], "final")
    store.flush_turns()
    yuki = store.add_conversation_fact("commitment", "Send the message tagging Hemanth", owner="yuki",
                                       source_turn_ids=[tid], at=T0, origin="model")
    user = store.add_conversation_fact("commitment", "The user will have Yuki tag Hemanth", owner="user",
                                       source_turn_ids=[tid], at=T0, origin="model")
    other = store.add_conversation_fact("commitment", "Book the dentist", owner="user", at=T0, origin="model")
    store.end_conversation_fact(yuki, "done", at=T0 + 60, note="The message was sent to Hemanth")
    assert close_mirrors(store, now=T0 + 120) == [{"item": user, "mirror": yuki}]
    ended = store.conversation_fact(user)
    assert ended.status == "done" and "mirror commit:" in ended.end_note and "sent to Hemanth" in ended.end_note
    assert store.conversation_fact(other).status == "active"


def test_the_check_closes_only_on_cited_newer_evidence(store):
    from yuki.memory.commitments import CommitmentChecker

    c1 = store.add_conversation_fact("commitment", "Send 15 emails to the leads", owner="user", at=T0,
                                     origin="model")
    c2 = store.add_conversation_fact("commitment", "Deposit 20,000 to the account", owner="user", at=T0,
                                     origin="model")
    c3 = store.add_conversation_fact("commitment", "Call the bank", owner="user", at=T0, origin="model")
    early = store.add_journal(T0 - 3600, None, "Gmail", "The user drafted emails to leads.", 5)
    late = store.add_journal(T0 + 3600, None, "Gmail", "The user sent 15 emails to the leads.", 6)
    joke = store.add_journal(T0 + 3700, None, "Discord", "The user joked they deposited a billion.", 3)
    store._write(lambda conn: conn.execute("UPDATE journal SET nature='humor' WHERE id=?", (joke,)))
    answer = {"decisions": [
        {"commitment": c1, "status": "done", "journal_ids": [late], "turn_ids": [], "reason": "sent them"},
        {"commitment": c2, "status": "done", "journal_ids": [joke, early], "turn_ids": [], "reason": "a joke"},
        {"commitment": c3, "status": "open", "journal_ids": [], "turn_ids": [], "reason": "nothing later"},
    ]}
    worker = _Worker(store, answer)
    result = CommitmentChecker(worker).check(T0 + 7200, force=True)
    assert result.closed == [{"item": c1, "status": "done"}]
    assert any(f"C{c2}" in r for r in result.rejected)    # a joke and an older entry close nothing
    note = store.conversation_fact(c1).end_note
    assert note.startswith("done, by Yuki's to-do check") and f"J{late}" in note
    assert store.conversation_fact(c2).status == "active" and store.conversation_fact(c3).status == "active"
    # at most every 30 minutes, and only with evidence newer than the last check
    checker = CommitmentChecker(worker)
    assert not checker.due(T0 + 7300)
    store.add_journal(T0 + 9000, None, "Gmail", "a new entry", 5)
    assert checker.due(T0 + 7200 + 1801)


def test_a_duplicate_of_a_done_commitment_is_closed_with_it(store):
    """09 §3.5: "Send 15 emails" (#34) stayed open beside its done duplicate (#35)."""
    from yuki.memory.commitments import CommitmentChecker

    dup = store.add_conversation_fact("commitment", "Send 15 emails", owner="user", at=T0, origin="call")
    done = store.add_conversation_fact("commitment", "Send 15 emails to the customers today", owner="user",
                                       at=T0 + 10, origin="call")
    store.end_conversation_fact(done, "done", at=T0 + 3600, note="sent")
    old = store.add_conversation_fact("commitment", "Pay the rent", owner="user", at=T0 + 7200, origin="call")
    store.add_journal(T0 + 7300, None, "Gmail", "a newer entry", 5)
    answer = {"decisions": [
        {"commitment": dup, "status": "done", "journal_ids": [], "turn_ids": [], "done_ids": [done],
         "reason": "the same task, done"},
        {"commitment": old, "status": "done", "journal_ids": [], "turn_ids": [], "done_ids": [done],
         "reason": "wrongly matched"}]}
    worker = _Worker(store, answer)
    result = CommitmentChecker(worker).check(T0 + 8000, force=True)
    assert result.closed == [{"item": dup, "status": "done"}] and any(f"C{old}" in r for r in result.rejected)
    assert f"commit:{done}" in store.conversation_fact(dup).end_note
    assert f"C{done} (done" in worker.calls[0]          # shown under DONE RECENTLY


def test_a_call_commitment_the_digest_marks_a_joke_is_ended_as_a_wrong_capture(store):
    from yuki.memory.commitments import close_joke_commitments

    said = T0 + 300.0
    cid = store.start_call(app="Discord", process="discord", via="screen", at=T0)
    store.update_call(cid, ended_at=T0 + 3600)
    _digest(store, cid, [{"call_id": cid, "start": said, "end": said + 5, "topic": "banter",
                          "lines": [{"at": said, "text": "send me the offer letter by 11:55", "nature": "humor"}]}],
            "Banter with Hemanth.", T0, T0 + 3600, T0 + 4000)
    joke = store.add_conversation_fact("commitment", "Send Quasar40404 an offer letter by 23:55", origin="call",
                                       owner="user", at=said, quote="send me the offer letter by 11:55")
    plan = store.add_conversation_fact("commitment", "Setup call with Hemanth on Sunday", origin="call",
                                       owner="user", at=said + 600, quote="come on Sunday for the setup")
    ended = close_joke_commitments(store, now=T0 + 7200)
    assert ended == [{"item": joke, "call": cid}]
    assert store.conversation_fact(joke).status == "wrong" and "marks the words it came from as a joke" in \
        store.conversation_fact(joke).end_note
    assert store.conversation_fact(plan).status == "active"


def _digest(store: Store, cid: int, blocks: list, gist: str, start: float, end: float, written: float) -> None:
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist=gist, blocks=blocks,
                           started_at=start, ended_at=end, speech_s=60.0, utterances=len(blocks), kept=len(blocks),
                           model="m")
    store._write(lambda conn: conn.execute("UPDATE call_digests SET updated_at=? WHERE call_id=?", (written, cid)))


def test_call_side_commitments_carry_the_nature_flag():
    """A to-do never comes from words said in jest: the commitment's own label, or a joke fact of the same line."""
    from yuki.memory.journal import RECORD_CALL_SIDE_TOOL, JournalWorker

    items = RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["commitments"]["items"]
    assert "nature" in items["required"] and "plans they made with someone" in \
        RECORD_CALL_SIDE_TOOL["input_schema"]["properties"]["commitments"]["description"]
    utter = [SimpleNamespace(utterance={"text": t}, at=T0 + i, call_app="Discord") for i, t in enumerate([
        "send me the offer letter by 11:55", "come on the call Sunday, we'll do the setup", "I'll send the deck"])]
    response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="record_call_side", input={
        "facts": [{"text": "joked about an offer letter", "nature": "humor", "importance": 3, "source": 1}],
        "commitments": [
            {"text": "Send an offer letter by 23:55", "title": "", "quote": "send me the offer letter by 11:55",
             "due": "", "source": 1, "subject": "offer letter", "nature": "fact"},
            {"text": "Setup call with Hemanth on 2026-10-04", "title": "", "quote": "we'll do the setup",
             "due": "2026-10-04", "source": 2, "subject": "setup call", "nature": "fact"},
            {"text": "Send the deck", "title": "", "quote": "I'll send the deck", "due": "", "source": 3,
             "subject": "deck", "nature": "humor"}]})], stop_reason="tool_use")
    worker = JournalWorker.__new__(JournalWorker)
    kept, rejected = worker._parse_commitments(response, SimpleNamespace(app="Discord"), utter, "record_call_side")
    assert [c.text for c in kept] == ["Setup call with Hemanth on 2026-10-04"] and len(rejected) == 2
    assert kept[0].due_at == datetime(2026, 10, 4).timestamp()


# -- P0.6: the follow-up scan reads digests and plans, and every event is vetted -------------------------------


class _NudgeWorker:
    def __init__(self, store: Store, answers: dict[str, Any]) -> None:
        self.store = store
        self.model = "m"
        self.answers = answers
        self.sent: dict[str, str] = {}

    def _rate_ok(self, now, cfg):
        return True

    def config(self):
        return SimpleNamespace()

    def _system(self, prompt, now):
        return prompt

    def _tool_call(self, model, system, user, tool, max_tokens, checkin):
        self.sent[tool["name"]] = user
        checkin.outcome = "ok"
        return self.answers[tool["name"]], SimpleNamespace(content=[]), {"messages": []}

    def _log(self, *a, **k):
        pass

    def _usage(self, checkin):
        return {}

    def _seal(self, value):
        return "sealed"

    def _log_checkin(self, *a, **k):
        pass


def test_followup_scan_reads_call_digests_and_plans_and_keeps_only_vetted_events(store):
    from yuki.memory.presence import PresenceWorker

    now = T0 + 3 * 3600
    cid = store.start_call(app="Discord", process="discord", via="screen", at=T0)
    store.update_call(cid, ended_at=T0 + 1800)
    blocks = [{"call_id": cid, "start": T0 + 60, "end": T0 + 70, "topic": "plans",
               "lines": [{"at": T0 + 60, "text": "come on the call Sunday for the setup", "nature": "fact"},
                         {"at": T0 + 65, "text": "and bring me a billion dollars", "nature": "humor"}]}]
    _digest(store, cid, blocks, "Plans with Hemanth.", T0, T0 + 1800, T0 + 2000)
    plan = store.add_conversation_fact("commitment", "Setup call with Hemanth on Sunday", origin="call", owner="user",
                                       at=T0 + 60, due_at=datetime(2026, 10, 4).timestamp())
    store.set_nudge_state("presence_events_scan", f"0|{T0 - 86400}||{T0}")
    w = _NudgeWorker(store, {
        "record_events": {"events": [
            {"what": "setup call with Hemanth", "when": "2026-10-04", "evidence": [f"D{cid}", f"C{plan}"]},
            {"what": "Demo Fair demo", "when": "2026-10-09", "evidence": [f"C{plan}"]}]},
        "vet_events": {"decisions": [{"n": 1, "keep": True, "why": "his own call with a friend"},
                                     {"n": 2, "keep": False, "why": "work"}]}})
    worker = PresenceWorker(w)
    assert worker._scan_events(now)
    sent = w.sent["record_events"]
    assert f"D{cid}" in sent and "come on the call Sunday" in sent and "billion" not in sent and f"C{plan}" in sent
    assert "come on the call Sunday" in w.sent["vet_events"]       # the vet sees the lines each event rests on
    events = worker._events()
    assert [e["what"] for e in events] == ["setup call with Hemanth"]


def test_a_failed_vet_keeps_nothing_and_reads_the_same_facts_again(store):
    from yuki.memory.presence import PresenceWorker

    now = T0 + 3600
    fid = store.add_journal(T0, None, "WhatsApp", "The user has a physics exam on 2026-10-10.", 7)
    store._write(lambda conn: conn.execute("UPDATE journal SET created_at=? WHERE id=?", (T0 + 60, fid)))

    class Failing(_NudgeWorker):
        def _tool_call(self, model, system, user, tool, max_tokens, checkin):
            if tool["name"] == "vet_events":
                raise RuntimeError("vet failed")
            return super()._tool_call(model, system, user, tool, max_tokens, checkin)

    w = Failing(store, {"record_events": {"events": [{"what": "physics exam", "when": "2026-10-10",
                                                      "evidence": [f"J{fid}"]}]}})
    store.set_nudge_state("presence_events_scan", f"0|{T0 - 86400}||{T0 - 86400}")
    scanner = PresenceWorker(w)
    scanner._scan_events(now)
    assert scanner._events() == []
    assert (store.nudge_state("presence_events_scan") or "").startswith("0|")
