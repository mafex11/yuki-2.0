"""The 3 Oct feedback, memory side (Builder A; docs/research/revamp-2026-10-02/38-feedback-root-causes.md and
43-build-appA-memory.md): migration 33, the shown forms, the pin, the direct "maybe misheard" and "To review" ops, the
people cards, the day digests and the dreaming tuning.

Offline: temp databases, invented people and scripted model answers; no network, no real memory.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from yuki.memory import titles
from yuki.memory.api import MemoryClient
from yuki.memory.store import MIGRATIONS, FactChange, ModelCall, NewFact, Store, WorkOp, _split_sql

FIXTURES = Path(__file__).parent / "fixtures"
NOW = time.time()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """The bits of PortraitWorker the passes use, with scripted model answers."""

    def __init__(self, store, answers=()):
        self.store, self.answers = store, list(answers)
        self._run_lock = threading.Lock()
        self.calls, self.logs = [], []

    def _call(self, **kw):
        self.calls.append(kw)
        kw["stats"].calls += 1
        kw["stats"].cost_usd += 0.001
        return self.answers.pop(0)

    def _log(self, type, **fields):
        self.logs.append((type, fields))


def _fact(store, kind, subject, text, *, origin="model", confidence=0.8, importance=5, shown=None, summary=None,
          sources=(), at=None):
    [(ok, fid)] = store.commit_portrait_changes(None, [FactChange(
        "ADD", None, kind, subject, text, confidence, list(sources), at or NOW, origin=origin, importance=importance,
        summary=summary, shown=shown, aspect="story" if kind == "personality" else None)])
    assert ok
    return fid


def _call_row(store, *, started_at, people=(), where="", place=None, gist=None):
    cid = store._write(lambda c: int(c.execute(
        "INSERT INTO calls(app, via, detected_at, status, updated_at) VALUES ('Discord','mic',?,'ended',?)",
        (started_at, started_at)).lastrowid))
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist=gist, blocks=[
        {"start": started_at, "end": started_at + 60, "topic": "a game night", "lines": []}],
        started_at=started_at, ended_at=started_at + 600, speech_s=60, utterances=3, kept=3, model="m",
        context={"people": [{"name": n, "how": "on_call_screen"} for n in people], "where": where,
                 **({"place": place} if place else {})})
    return cid


def _journal(store, text, *, at, nature="fact", source=None, app="Discord"):
    call = ModelCall(at=at, thread_id=None, model="test", cost_usd=0.0)
    store.commit_batch(call, [], [NewFact(at=at, thread_id=None, app=app, host=None, fact=text, importance=5,
                                          nature=nature, source=source)])
    return max(j.id for j in store.journal_between(None, None))


# -- migration 33 ---------------------------------------------------------------------------------------------


def test_migration_33_is_additive_from_32():
    with tempfile.TemporaryDirectory() as tmp:
        db2 = Path(tmp) / "old.db"                # a database as it was at migration 32
        conn = sqlite3.connect(str(db2), isolation_level=None)
        for index in range(32):
            for statement in _split_sql(MIGRATIONS[index]):
                conn.execute(statement)
        conn.execute("PRAGMA user_version=32")
        conn.execute("INSERT INTO calls(app, via, detected_at, status, updated_at) VALUES ('Discord','mic',1,'ended',1)")
        conn.close()
        s = Store.open(db2)
        try:
            assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS) == 33
            cols = {t: {r[1] for r in s._query(f"PRAGMA table_info({t})")} for t in
                    ("portrait_facts", "work_threads", "call_digests", "journal", "contact_routes", "day_digests")}
            assert {"shown_ciphertext", "pinned", "reviewed_at"} <= cols["portrait_facts"]
            assert "shown_ciphertext" in cols["work_threads"] and "gist_shown_ciphertext" in cols["call_digests"]
            assert "reviewed_at" in cols["journal"] and "use_ciphertext" in cols["contact_routes"]
            assert {"day", "text_ciphertext", "shown_ciphertext", "refs"} <= cols["day_digests"]
            assert s.call_record(1).app == "Discord"                       # old rows untouched
        finally:
            s.close()


def test_migration_33_sets_the_pin_for_user_facts_only(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    for index in range(32):
        for statement in _split_sql(MIGRATIONS[index]):
            conn.execute(statement)
    conn.execute("PRAGMA user_version=32")
    conn.close()
    # rows written by the store at version 32 need its key: write them through a store opened at 33, then reset the
    # columns migration 33 added and run its UPDATE again, as the migration does on the real file
    s = Store.open(db)
    try:
        told = _fact(s, "work", "Acme", "The user works at Acme.", origin="user")
        seen = _fact(s, "interest", "Chess", "The user plays chess most evenings.")
        s._write(lambda c: c.execute("UPDATE portrait_facts SET pinned=0"))
        for statement in _split_sql(MIGRATIONS[32]):
            if statement.lstrip().upper().startswith("UPDATE PORTRAIT_FACTS"):
                s._write(lambda c, st=statement: c.execute(st))
        assert s.portrait_fact(told).pinned is True and s.portrait_fact(seen).pinned is False
        assert s.portrait_fact(told).text == "The user works at Acme."     # nothing else moved
    finally:
        s.close()


# -- the pin ----------------------------------------------------------------------------------------------------


def test_pin_and_unpin_are_one_direct_op_with_a_diary_line_and_keep_the_provenance(store):
    client = MemoryClient(store)
    fid = _fact(store, "routine", "Evenings", "Most evenings the user plays chess from about 21:00.", origin="user")
    assert store.portrait_fact(fid).pinned                                   # the user said it: pinned as before
    out = client.pin_fact(f"fact:{fid}", on=False)
    assert out == {"ok": True, "ref": f"fact:{fid}", "pinned": False}
    f = store.portrait_fact(fid)
    assert not f.pinned and f.origin == "user"                               # "you told me" stays
    assert client.pin_fact(fid, on=True)["pinned"] is True and store.portrait_fact(fid).pinned
    assert [d["action"] for d in store.changelog(pass_="pin")] == ["pin", "unpin"]
    assert client.pin_fact("fact:99999")["ok"] is False


def test_a_new_version_keeps_the_pin_and_the_ops_protect_pinned_facts_not_origin(store):
    from yuki.memory.portrait import PortraitWorker

    old = _journal(store, "The user said they play chess most evenings.", at=NOW - 2 * 86400)
    jid = _journal(store, "The user said they play chess on Sundays now.", at=NOW - 3600)
    fid = _fact(store, "routine", "Chess", "The user plays chess most evenings.", at=NOW - 86400)
    store.set_fact_pinned(fid, True)
    journal = store.journal_between(None, None)
    worker = PortraitWorker(store, client=object())
    op = {"op": "UPDATE", "fact_id": fid, "kind": "routine", "subject": "Chess", "summary": None, "shown": None,
          "text": "The user plays chess on Sundays.", "confidence": 0.7, "importance": 5, "journal_ids": [old],
          "episode_ids": [], "based_on_facts": [], "reason": "r", "loop": None, "aspect": None}
    changes, outcomes = worker._validate([op], store.portrait_facts(), journal, NOW)
    assert not changes and "pinned" in outcomes[0].status                     # nothing later: refused
    store.set_fact_pinned(fid, False)                                         # unpinned: the model may update it
    assert len(worker._validate([op], store.portrait_facts(), journal, NOW)[0]) == 1
    store.set_fact_pinned(fid, True)
    changes, outcomes = worker._validate([dict(op, journal_ids=[jid])], store.portrait_facts(), journal, NOW)
    assert len(changes) == 1                                                  # later evidence may change it
    [(ok, new_id)] = store.commit_portrait_changes(None, changes)
    assert ok and store.portrait_fact(new_id).pinned                          # the pin follows the fact


# -- the shown forms ----------------------------------------------------------------------------------------------


def test_the_portrait_operations_write_the_shown_form_with_the_fact(store):
    from yuki.memory.portrait import AUDIT_PORTRAIT_TOOL, UPDATE_PORTRAIT_TOOL, PortraitWorker

    for tool in (UPDATE_PORTRAIT_TOOL, AUDIT_PORTRAIT_TOOL):
        item = tool["input_schema"]["properties"]["operations"]["items"]
        assert "shown" in item["required"] and "shown" in item["properties"]
    jid = _journal(store, "The user said they teach Kiran maths on weekends.", at=NOW - 60)
    worker = PortraitWorker(store, client=object())
    changes, _ = worker._validate([{
        "op": "ADD", "fact_id": None, "kind": "work", "subject": "Kiran", "summary": "Teaches Kiran maths",
        "shown": "You teach your friend Kiran maths on weekends.", "text": "The user teaches Kiran maths on weekends.",
        "confidence": 0.8, "importance": 5, "journal_ids": [jid], "episode_ids": [], "based_on_facts": [],
        "reason": "said", "loop": None, "aspect": None}], store.portrait_facts(), store.journal_between(None, None), NOW)
    [(ok, fid)] = store.commit_portrait_changes(None, changes)
    f = store.portrait_fact(fid)
    assert f.shown == "You teach your friend Kiran maths on weekends."
    raw = store._query("SELECT shown_ciphertext FROM portrait_facts WHERE id=?", (fid,))[0][0]
    assert "Kiran" not in raw                                                 # encrypted like the text
    assert store.shown_missing()["fact"] == []


def test_journal_lines_for_the_operations_say_who_was_on_the_call(store):
    from yuki.memory.portrait import PortraitWorker

    cid = _call_row(store, started_at=NOW - 7200, people=["Ana", "Sam"],
                    place={"app": "Discord", "kind": "voice", "channel": "late-night", "server": "Ana's server"})
    jid = _journal(store, "The user said they became guarded after Leo left the group.", at=NOW - 7000,
                   source={"kind": "call", "by": "the user", "where": "Discord call", "call": f"call:{cid}"})
    worker = PortraitWorker(store, client=object())
    text = worker._ops_message(kind="nightly", now=NOW, facts=[], journal=store.journal_by_ids([jid]), activity="",
                               part=(1, 1))
    line = next(x for x in text.splitlines() if x.startswith(f"J{jid} "))
    assert "[call: Discord, #late-night voice (Ana's server) call on" in line and "with Ana, Sam]" in line


def test_the_titles_pass_writes_shown_forms_for_facts_threads_and_gists(store):
    cid = _call_row(store, started_at=NOW - 7200, people=["Ana", "Sam"], where="Discord, voice channel late-night",
                    gist="A heated game where they chose killing people for fun, which shocked the user.")
    jid = _journal(store, "In a Discord call the user said they became guarded after Leo left.", at=NOW - 7000,
                   source={"kind": "call", "where": "Discord call", "call": f"call:{cid}"})
    fid = _fact(store, "person", "Leo", "In a Discord call on 2026-09-30 the user said they became guarded after Leo "
                                        "left the group.", sources=[jid])
    [op] = store.apply_work([WorkOp("starts", None, title="Maths for Kiran", state="The user prepared chapter 1.",
                                    at=NOW - 600)])
    tid = op.result_id
    worker = _Worker(store, [{"items": [
        {"id": f"P{fid}", "shown": "On 30 September, on a Discord call with other friends, you said you became "
                                   "guarded after Leo left the group."},
        {"id": f"W{tid}", "shown": "You prepared chapter 1."},
        {"id": f"G{cid}", "shown": "A heated game where your friends chose killing people for fun, which shocked you."},
    ]}])
    r = titles.Titler(worker).run(what="shown", now=NOW)
    assert r.ok and r.shown_pending == 3 and r.shown_written == 3 and r.calls == 1
    user = worker.calls[0]["user"]
    assert "CALLS it rests on: Discord, voice channel late-night call on" in user and "with Ana, Sam" in user
    assert "WHO WAS ON IT: Ana, Sam" in user and "TOPICS: a game night" in user
    assert store.portrait_fact(fid).shown.startswith("On 30 September, on a Discord call with other friends")
    assert store.work_threads(None)[0].shown == "You prepared chapter 1."
    assert store.call_digest(cid)["gist_shown"].startswith("A heated game where your friends")
    assert not any(store.shown_missing().values())
    # a new state makes the shown one stale: cleared, written again later
    store.apply_work([WorkOp("continues", tid, state="The user finished chapter 2.", at=NOW)])
    assert store.work_threads(None)[0].shown is None and store.shown_missing()["thread"] == [tid]


def test_a_dry_run_writes_nothing_and_returns_the_forms_for_a_spot_check(store):
    fid = _fact(store, "work", "Acme", "The user works at Acme.")
    worker = _Worker(store, [{"items": [{"id": f"P{fid}", "shown": "You work at Acme."}]}])
    r = titles.Titler(worker).run(what="shown", dry_run=True)
    assert r.shown == [{"ref": f"P{fid}", "text": "The user works at Acme.", "context": "", "shown": "You work at Acme."}]
    assert store.portrait_fact(fid).shown is None and r.run_id is None


def test_shown_forms_are_kept_whole_or_not_at_all():
    assert titles.clean_shown('  "You work at Acme."  ') == "You work at Acme."
    assert titles.clean_shown("You said you were…") is None and titles.clean_shown("") is None
    assert titles.clean_shown("x" * (titles.SHOWN_MAX_CHARS + 1)) is None


ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CLOCK_24 = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b(?!\s*[AaPp]\.?[Mm])")   # a clock time without AM or PM
THE_USER = re.compile(r"\bthe user\b|\buser's\b", re.IGNORECASE)


def _shown_fixture() -> list[dict]:
    return json.loads((FIXTURES / "shown_sample.json").read_text(encoding="utf-8"))["items"]


def test_no_shown_text_in_the_fixture_set_has_an_iso_date_a_24_hour_time_or_the_user():
    """A test over the output, not a runtime filter (report 38 §2e.4): a dry run of the shown pass over invented
    facts (tests/fixtures/shown_sample.json, recorded from Haiku) and the worked examples of SHOWN_RULES."""
    examples = [line.split("-> ", 1)[1] for line in titles.SHOWN_EXAMPLES.splitlines()
                if line.startswith("- ") and "-> " in line]
    texts = [i["shown"] for i in _shown_fixture()] + examples
    assert len(texts) >= 12
    for t in texts:
        assert not ISO_DATE.search(t), t
        assert not CLOCK_24.search(t), t
        assert not THE_USER.search(t), t


def test_the_fixture_set_names_friends_and_says_when_the_person_was_not_on_the_call():
    items = {i["ref"]: i for i in _shown_fixture()}
    p3 = items["P3"]["shown"]                               # Leo was not on the call with Ana and Sam
    assert ("other friends" in p3 or "Ana and Sam" in p3) and "call with Leo" not in p3
    assert "your friends" in items["G1"]["shown"].lower() and "shocked you" in items["G1"]["shown"]
    assert "your father" in items["P2"]["shown"].lower()
    assert "September" in items["P1"]["shown"] and "AM" in items["P1"]["shown"]


# -- maybe misheard ----------------------------------------------------------------------------------------------


def test_misheard_lists_the_voice_channel_and_resolves_at_once_without_a_model(store, monkeypatch):
    import anthropic

    def no_model(*a, **k):                       # nothing on the click path may build a model client
        raise AssertionError("a model client on the click path")

    monkeypatch.setattr(anthropic, "Anthropic", no_model, raising=False)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", no_model, raising=False)
    client = MemoryClient(store)
    cid = _call_row(store, started_at=NOW - 7200, people=["Ana"],
                    place={"app": "Discord", "kind": "voice", "channel": "late-night", "server": "Ana's server"})
    heard = _journal(store, "Said what sounded like 'the build is green'.", at=NOW - 7000, nature="uncertain",
                     source={"kind": "call", "where": "Discord call", "call": f"call:{cid}", "unclear": True})
    other = _journal(store, "Said what sounded like a name.", at=NOW - 6000, nature="uncertain", app="WhatsApp")
    m = client.misheard(now=NOW)
    assert m["total"] == 2 and [i["id"] for i in m["items"]] == [other, heard]            # newest first
    item = m["items"][1]
    assert item["ref"] == f"journal:{heard}" and item["call"] == f"call:{cid}" and item["where"] == "Discord call"
    assert item["heard_in"] == "Discord, #late-night voice (Ana's server)" and item["channel_known"]
    assert item["people"] == ["Ana"] and m["items"][0]["heard_in"] == "WhatsApp"
    t0 = time.perf_counter()
    out = client.resolve_heard(f"journal:{heard}", "right")
    assert time.perf_counter() - t0 < 1.0
    assert out["ok"] and out["nature"] == "fact" and out["correction_id"] is None
    assert [i["id"] for i in client.misheard(now=NOW)["items"]] == [other]                 # gone at once
    assert store.journal_by_ids([heard])[0].nature == "fact"
    assert client.resolve_heard(f"journal:{heard}", "undo")["ok"]                          # Undo brings it back
    assert len(client.misheard(now=NOW)["items"]) == 2
    out = client.resolve_heard(other, "wrong")
    assert out["ok"] and out["correction_id"]
    assert [i["id"] for i in client.misheard(now=NOW)["items"]] == [heard]
    assert store.journal_by_ids([other]) == [] or store.journal_by_ids([other])[0].id == other
    assert store._query("SELECT status FROM correction_passes WHERE correction_id=?",
                        (out["correction_id"],))[0][0] == "queued"                         # the ripple, in the service
    undo = client.resolve_heard(other, "undo", correction_id=out["correction_id"])
    assert undo["ok"] and undo["cancelled"]
    assert store._query("SELECT status FROM correction_passes WHERE correction_id=?",
                        (out["correction_id"],))[0][0] == "skipped"
    assert len(client.misheard(now=NOW)["items"]) == 2
    assert {d["action"] for d in store.changelog(pass_="heard")} == {"right", "wrong", "undo"}


# -- the "To review" queue ---------------------------------------------------------------------------------------------


def test_the_review_queue_holds_at_most_five_a_day_and_answers_are_direct(store):
    client = MemoryClient(store)
    ids = [_fact(store, "interest", f"Topic {n}", f"The user likes topic {n}.", confidence=0.3 + n * 0.05,
                 importance=6, summary=f"Likes topic {n}", shown=f"You like topic {n}.") for n in range(8)]
    _fact(store, "work", "Acme", "The user works at Acme.", origin="user")                 # told: never reviewed
    _fact(store, "open_loop", "Kiran", "Send Kiran the notes (asked by Kiran in Slack).")    # a to-do, not here
    q = client.review_queue(now=NOW)
    assert q["cap"] == 5 and q["left_today"] == 5 and q["waiting"] == 8 and len(q["items"]) == 5
    first = q["items"][0]
    assert first["ref"] == f"fact:{ids[0]}" and first["flag"] == "unsure" and first["text"] == "You like topic 0."
    assert first["headline"] == "Likes topic 0"
    assert client.review_mark(first["ref"], "right")["ok"]
    assert store.portrait_fact(ids[0]).pinned and store.portrait_fact(ids[0]).reviewed_at
    wrong = client.review_mark(q["items"][1]["ref"], "wrong")
    assert wrong["ok"] and wrong["correction_id"] and store.portrait_fact(ids[1]).valid_to is not None
    q2 = client.review_queue(now=NOW)
    assert q2["reviewed_today"] == 2 and q2["left_today"] == 3 and len(q2["items"]) == 3
    assert all(i["ref"] not in (f"fact:{ids[0]}", f"fact:{ids[1]}") for i in q2["items"])
    undo = client.review_mark(f"fact:{ids[1]}", "undo", correction_id=wrong["correction_id"])
    assert undo["ok"] and undo["cancelled"] and store.portrait_fact(ids[1]).valid_to is None
    assert client.review_queue(now=NOW)["left_today"] == 4
    assert client.review_mark("fact:abc", "right")["ok"] is False


# -- people ----------------------------------------------------------------------------------------------------------


def test_a_card_has_a_human_usual_line_one_summary_and_the_relation_from_the_summary(store):
    from yuki.memory import contacts

    fid = _fact(store, "person", "Dev", "Dev is the user's father; they talk about future plans.",
                summary="Your father, talks future plans with you",
                shown="Your father Dev talks with you about your future plans.")
    other = _fact(store, "person", "Dev", "Dev asked the user to call on Sunday.", summary="Asked you to call Sunday",
                  shown="Dev asked you to call on Sunday.")
    route = contacts.Route(id=7, contact_id=1, key="k", service="Discord", kind="dm", chat="dev", handle="@dev",
                           thread_ids=[], use="Plans for the family trip.")
    person = contacts.Contact(id=1, key="dev", name="Dev", relation="the user's father", aliases=[], tone="short",
                              fact_ids=[fid, other], routes=[route])
    card = contacts.card(store, person, with_samples=0)
    usual = card["usual_route"]
    assert usual["label"] == "Discord DM" and usual["sub"] == "@dev"
    assert usual["use"] == "Plans for the family trip." and usual["tone"] == "short"
    assert "route:" not in json.dumps(usual["label"]) and card["usual_ref"] == "route:7"
    # a window that prints ``usual`` as it is gets a plain line, never an object (review A2)
    assert card["usual"] == "Discord DM. Plans for the family trip."
    assert contacts.usual_line({**usual, "use": None}) == "Discord DM \u00b7 @dev" and contacts.usual_line(None) is None
    assert card["summary"] == {"ref": f"fact:{fid}", "headline": "Your father, talks future plans with you",
                               "text": "Your father Dev talks with you about your future plans.", "pinned": False}
    assert card["relation"] == "Your father, talks future plans with you"           # never "the user's father"
    assert [f["ref"] for f in card["facts_shown"]] == [f"fact:{other}"]              # the summary's fact once only
    assert contacts.card_text(card).startswith("Dev - the user's father")            # the agent keeps memory's form
    assert contacts.route_use("one two three four five six seven eight nine ten eleven twelve thirteen") is None
    assert contacts.route_use('"Code questions and review links."') == "Code questions and review links."


def test_a_person_fact_written_after_the_link_run_still_attaches_by_name(store):
    from yuki.memory import contacts

    fid = _fact(store, "person", "Mira", "Mira is the user's classmate.", summary="Your classmate")
    person = contacts.Contact(id=2, key="mira", name="Mira", relation=None, aliases=[], tone=None, fact_ids=[])
    card = contacts.card(store, person, with_samples=0)
    assert card["summary"]["ref"] == f"fact:{fid}" and card["usual"] is None and card["usual_route"] is None


# -- call digests ---------------------------------------------------------------------------------------------------


def test_the_digest_keeps_a_structured_place_only_as_the_screen_shows_it_and_the_shown_gist():
    from yuki.memory.calldigest import WRITE_TIMELINE_TOOL, parse_place, parse_shown

    item = WRITE_TIMELINE_TOOL["input_schema"]["properties"]["calls"]["items"]
    assert {"gist_shown", "place"} <= set(item["required"])
    ctx = "Voice Connected / late-night / Ana's server / Ana / Sam"
    assert parse_place({"app": "Discord", "kind": "voice", "channel": "#late-night", "server": "Ana's server"},
                       ctx + " Discord") == {"app": "Discord", "channel": "#late-night", "server": "Ana's server",
                                             "kind": "voice"}
    assert parse_place({"app": "", "kind": "voice", "channel": "made-up", "server": ""}, ctx) is None
    answer = {"calls": [{"call": 4, "gist_shown": "You and your friends planned a trip."},
                        {"call": 9, "gist_shown": "Not this call."}]}
    assert parse_shown(answer, [4]) == {4: "You and your friends planned a trip."}
    assert titles.place_label({"app": "Discord", "kind": "voice", "channel": "late-night", "server": "S"}) == \
        "Discord, #late-night voice (S)"
    assert titles.place_label(None, "Discord, voice channel X") == "Discord, voice channel X"


# -- day digests and the new chat each day -------------------------------------------------------------------------------


def test_day_digests_are_written_from_the_day_and_connect_the_next_days_chat(store):
    from yuki.memory.daydigest import DayDigester, day_window, previous_days

    yesterday = previous_days(1, NOW)[0]
    start, _end = day_window(yesterday)
    [op] = store.apply_work([WorkOp("starts", None, title="Replies fix", state="The user merged the replies fix.",
                                    at=start + 5 * 3600, seen_at=start + 5 * 3600)])
    worker = _Worker(store, [{"text": "The user merged the replies fix. Still open: send Priya the video.",
                              "shown": "You merged the replies fix. Still open: send Priya the video.",
                              "open": [{"text": "Send Priya the video", "thread": op.result_id},
                                       {"text": "Not a thread", "thread": 99999}]}])
    r = DayDigester(worker).run(days=[yesterday], now=NOW)
    assert r.ok and r.days == [yesterday] and r.calls == 1
    assert "Replies fix" in worker.calls[0]["user"] and "THE DAY:" in worker.calls[0]["user"]
    assert DayDigester(worker).run(days=[yesterday], now=NOW).calls == 0            # once a day
    client = MemoryClient(store)
    c = client.day_connector(now=NOW)
    assert c["day"] == yesterday and c["label"] == "Yesterday"
    assert c["text"] == "You merged the replies fix. Still open: send Priya the video."
    assert c["open"] == [{"text": "Send Priya the video", "thread": f"thread:{op.result_id}"},
                         {"text": "Not a thread", "thread": None}]
    resume = client._resume_from_day(NOW)
    assert "merged the replies fix" in resume and "this is a new chat" in resume
    assert len(resume) <= 1000


def test_a_day_with_nothing_in_it_costs_no_call_and_the_connector_asks_lazily(store):
    from yuki.memory.daydigest import DAYDIGEST_FLAG, DayDigester, day_window, previous_days

    worker = _Worker(store, [])
    assert DayDigester(worker).run(back=3, now=NOW).calls == 0
    yesterday = previous_days(1, NOW)[0]
    start, _ = day_window(yesterday)
    _journal(store, "The user opened a page.", at=start + 3600)
    client = MemoryClient(store)
    assert client.day_connector(now=NOW) is None
    from yuki.memory.store import flag_path

    assert flag_path(store.path, DAYDIGEST_FLAG).exists()                           # the service builds it


def test_chat_days_and_turns_on_group_exchanges_by_the_users_day(store):
    from yuki.memory.daydigest import day_window, previous_days

    client = MemoryClient(store)
    yesterday = previous_days(1, NOW)[0]
    start, _ = day_window(yesterday)
    client.log_turn("s1", 1, start + 3600, "open my notes", "Done.", [], "final")
    client.log_turn("s1", 2, start + 23 * 3600 + 1800, "and the music", "Done.", [], "final")   # 03:30: still that day
    client.log_turn("s2", 3, NOW - 60, "what's next", "The video for Priya.", [], "final")
    store.flush_turns(5.0)
    days = client.chat_days(now=NOW)
    assert [d["count"] for d in days if d["day"] == yesterday] == [2]
    assert [t["user"] for t in client.turns_on(yesterday)] == ["open my notes", "and the music"]


# -- dreaming ---------------------------------------------------------------------------------------------------------


def test_dreaming_shows_closer_look_alikes_and_merges_a_behaviour_into_a_routine(store):
    from yuki.memory import dreaming

    assert dreaming.LOOKALIKE_COSINE == 0.60 and dreaming.MAX_CHANGES == 16
    assert dreaming.mergeable({"behaviour", "routine"}) and dreaming.mergeable({"work"})
    assert not dreaming.mergeable({"work", "interest"})
    b = _fact(store, "behaviour", "Chess nights", "The user played chess on 2026-10-01 night.")
    r = _fact(store, "routine", "Chess nights", "Most nights the user plays chess from about 22:00.")
    d = dreaming.Dreamer(_Worker(store))
    facts = store.portrait_facts()
    changes, _ratings, rejected = d.validate([{"action": "merge", "fact_ids": [b, r], "keep_id": r,
                                               "text": "Most nights the user plays chess from about 22:00.",
                                               "importance": 5, "journal_ids": [], "reason": "the same nights"}],
                                             facts, set())
    assert not rejected and changes[0]["keep"] == r
    store.set_fact_pinned(r, True)
    _c, _r, rejected = d.validate([{"action": "retire", "fact_ids": [r], "keep_id": None, "text": "",
                                    "importance": None, "journal_ids": [], "reason": "stale"}],
                                  store.portrait_facts(), set())
    assert rejected and "pinned" in rejected[0]


# -- review fixes (report 43, "Review fixes") ------------------------------------------------------------------------


def _scheduler(store, worker, *, clock=None, logs=None):
    from yuki.memory.portrait import PortraitScheduler

    logs = [] if logs is None else logs
    return PortraitScheduler(store, worker, db_path=store.path, log=lambda t, **f: logs.append((t, f)),
                             idle_fn=lambda: 1e9, clock=clock or (lambda: NOW))


def test_the_day_that_just_ended_gets_its_digest_at_the_rollover_and_failures_back_off(store):
    """A1: the nightly slot (22:00) comes before the user day ends (04:00), so the first check of each new day
    writes the day that just ended. A6: a day whose call keeps failing is tried at most three times a run."""
    from yuki.memory.daydigest import MAX_TRIES_PER_DAY, day_window, previous_days, today

    yesterday = previous_days(1, NOW)[0]
    start, end = day_window(yesterday)
    _journal(store, "The user opened the garden planner.", at=start + 3600)
    store.apply_work([WorkOp("starts", None, title="Garden planner", state="The user drew the beds.",
                             at=start + 3600, seen_at=start + 3600)])
    # 22:05 on that day: the nightly slot's pass cannot cover it yet (it is not over)
    assert yesterday not in previous_days(2, end - 6 * 3600 + 300)
    failing = _Worker(store, [None] * (MAX_TRIES_PER_DAY + 2))
    sched = _scheduler(store, failing)
    assert sched._daydigest_rollover(NOW) and not sched._daydigest_rollover(NOW + 60)   # once per user day
    assert sched._daydigest_rollover(day_window(today(NOW))[1] + 60)                    # and again the next day
    for _ in range(MAX_TRIES_PER_DAY + 2):
        sched._daydigest("connector asked")
    assert len(failing.calls) == MAX_TRIES_PER_DAY                                    # then it stops asking
    good = _Worker(store, [{"text": "The user drew the garden beds.", "shown": "You drew the garden beds.",
                            "open": []}])
    sched = _scheduler(store, good)                                                    # a new service run
    assert sched._daydigest("day rollover") == "daydigest"
    assert [d["day"] for d in store.day_digests(days=[yesterday])] == [yesterday]
    assert sched._daydigest("day rollover") is None and len(good.calls) == 1          # written: no more calls


def test_a_digest_without_its_shown_form_is_retried_once(store):
    from yuki.memory.daydigest import DayDigester, day_window, previous_days

    yesterday = previous_days(1, NOW)[0]
    start, _ = day_window(yesterday)
    store.apply_work([WorkOp("starts", None, title="Garden planner", state="The user drew the beds.",
                             at=start + 3600, seen_at=start + 3600)])
    store.save_day_digest(yesterday, "The user worked on the garden planner.", None, {"exchanges": 0})
    worker = _Worker(store, [{"text": "The user worked on the garden planner.", "shown": "", "open": []}])
    assert DayDigester(worker).missing([yesterday]) == [yesterday]
    r = DayDigester(worker).run(days=[yesterday], now=NOW)
    assert r.calls == 1 and r.failed == [yesterday]
    [d] = store.day_digests(days=[yesterday])
    assert d["text"] == "The user worked on the garden planner." and d["refs"]["shown_retried"] is True
    assert DayDigester(worker).missing([yesterday]) == []                              # once only


def test_the_resume_block_never_calls_an_older_digest_the_last_day_and_asks_for_yesterdays(store):
    from yuki.memory.daydigest import DAYDIGEST_FLAG, day_window, previous_days
    from yuki.memory.store import flag_path

    yesterday, before = previous_days(2, NOW)
    store.save_day_digest(before, "The user planned the seed order.", "You planned the seed order.", {})
    start, _ = day_window(yesterday)
    client = MemoryClient(store)
    client.log_turn("s1", 1, start + 3600, "open the garden planner", "Done.", [], "final")
    store.flush_turns(5.0)
    resume = client._resume_from_day(NOW)
    assert "planned the seed order" in resume and "last day" not in resume
    assert "talked with you yesterday, whose digest is not written yet" in resume
    assert flag_path(store.path, DAYDIGEST_FLAG).exists()                             # the service builds it
    store.save_day_digest(yesterday, "The user drew the beds.", "You drew the beds.", {})
    assert MemoryClient(store)._resume_from_day(NOW).startswith("[Yesterday (")


def test_an_undo_after_the_correction_pass_started_keeps_the_answer(store):
    """A3: the restore depends on cancelling the queued pass; once the pass started, the item stays removed."""
    client = MemoryClient(store)
    fid = _fact(store, "interest", "Kites", "The user flies kites.", confidence=0.4, shown="You fly kites.")
    wrong = client.review_mark(f"fact:{fid}", "wrong")
    store._write(lambda c: c.execute("UPDATE correction_passes SET status='applying' WHERE correction_id=?",
                                     (wrong["correction_id"],)))
    undo = client.review_mark(f"fact:{fid}", "undo", correction_id=wrong["correction_id"])
    assert not undo["ok"] and undo["cancelled"] is False and "Too late" in undo["error"]
    assert store.portrait_fact(fid).valid_to is not None                             # still retired
    heard = _journal(store, "Said what sounded like 'kite day'.", at=NOW - 600, nature="uncertain")
    out = client.resolve_heard(f"journal:{heard}", "wrong")
    store._write(lambda c: c.execute("UPDATE correction_passes SET status='applying' WHERE correction_id=?",
                                     (out["correction_id"],)))
    undo = client.resolve_heard(f"journal:{heard}", "undo", correction_id=out["correction_id"])
    assert not undo["ok"] and "Too late" in undo["error"]
    assert heard not in [i["id"] for i in client.misheard(now=NOW)["items"]]


def test_one_answer_then_a_same_day_update_counts_once(store):
    """A4: a later version carries reviewed_at over; the daily cap counts answers, not versions."""
    client = MemoryClient(store)
    ids = [_fact(store, "interest", f"Topic {n}", f"The user likes topic {n}.", confidence=0.3, importance=6,
                 shown=f"You like topic {n}.") for n in range(3)]
    assert client.review_mark(f"fact:{ids[0]}", "right")["ok"]
    [(ok, new)] = store.commit_portrait_changes(None, [FactChange(
        "UPDATE", ids[0], "interest", "Topic 0", "The user likes topic 0 a lot.", 0.9, [], NOW + 5, "model")])
    assert ok and store.portrait_fact(new).reviewed_at is not None
    q = client.review_queue(now=NOW + 10)
    assert q["reviewed_today"] == 1 and q["left_today"] == 4


def test_the_shown_pass_keeps_its_own_attempt_count(store):
    """A5: a fact's label failing three times never blocks its shown form (both were keyed P<id>)."""
    fid = _fact(store, "interest", "Kites", "The user flies kites.", summary=None, shown=None)
    t = titles.Titler(_Worker(store))
    t._failed[f"P{fid}"] = titles.MAX_ATTEMPTS
    assert f"P{fid}" not in [p.ref for p in t.pending(only={"fact": [fid]})]
    assert [p.ref for p in t.pending_shown(only={"fact": [fid]}, now=NOW)] == [f"P{fid}"]


def test_an_undo_on_an_entry_never_answered_changes_nothing(store):
    """A7: Undo on an unanswered fact entry must not turn it into a "maybe misheard" one."""
    client = MemoryClient(store)
    jid = _journal(store, "The user opened the garden planner.", at=NOW - 600)
    out = client.resolve_heard(f"journal:{jid}", "undo")
    assert not out["ok"] and store.journal_by_ids([jid])[0].nature == "fact"
    assert client.misheard(now=NOW)["items"] == []


def test_a_titles_run_of_shown_forms_only_is_logged(store):
    """A8: the scheduler logs a pass that wrote only shown forms (model calls) and reports it."""
    _fact(store, "interest", "Kites", "The user flies kites.", summary="Flies kites", shown=None)
    logs = []
    sched = _scheduler(store, _Worker(store), logs=logs)

    class _Titler:
        def run(self, reason=""):
            return titles.TitlesResult(shown_pending=2, shown_written=2, calls=1, cost_usd=0.01)

    sched._titler = _Titler()
    assert sched._titles("idle") == "titles"
    [(_, fields)] = [entry for entry in logs if entry[0] == "portrait_run"]
    assert fields["kind"] == "titles" and fields["shown_pending"] == 2 and fields["shown_written"] == 2
