"""The Work page's memory side (docs/research/revamp-2026-10-02/68-build-appWork.md): migration 36 (work_items), the
reply candidates from the stored chats, the one-call check (chats, loops, meetings) validated in code, the board the
window reads, the user's marks, putting ended items back, and the add field's due words.

Temp databases and stub models only; every name and message is invented; nothing touches the desktop or a model.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store

NOW = datetime(2026, 10, 3, 21, 52).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def msg(fp: str, text: str, *, sender: str | None = "Riya", me: bool = False, at: float | None = None) -> Any:
    return SimpleNamespace(fingerprint=fp, content_key=fp, sender=sender, is_me=me, time_label="21:00", at=at,
                           text=text, day_label=None)


def chat(store: Store, scope: str, app: str = "Discord", process: str = "discord") -> int:
    return store.upsert_thread(app, scope, None, process=process, scope=scope, kind="conversation")


class _Worker:
    """What the check needs of the conversation worker: its store, model call, log and seal (an invented answer)."""

    def __init__(self, store: Store, answer: dict[str, Any]) -> None:
        self.store = store
        self.answer = answer
        self.calls: list[str] = []
        self.logged: list[tuple[str, dict]] = []
        self.settings = SimpleNamespace(user_names=["Sam"])

    def _call(self, purpose, system, user, tool, call, model=None):
        self.calls.append(user)
        call.cost_usd = 0.001
        return self.answer, SimpleNamespace(content=[]), {}

    def _log(self, type, **fields):
        self.logged.append((type, fields))

    def _seal(self, value):
        return "sealed"


def test_migration_36_adds_work_items(store):
    cols = {r[1] for r in store._query("PRAGMA table_info(work_items)")}
    assert {"key", "kind", "basis", "data_ciphertext", "judged_at", "mark", "mark_basis", "mark_at"} <= cols


def test_a_chat_is_a_reply_candidate_only_while_its_newest_message_is_not_yours(store):
    from yuki.memory.workboard import reply_candidates

    riya = chat(store, "Riya")
    store.add_conversation(riya, NOW - 600, "focus", [msg("a", "hey", at=NOW - 900), msg("b", "did you get the photos?", at=NOW - 700)])
    group = chat(store, "#general (Gamers)")
    store.add_conversation(group, NOW - 500, "focus", [msg("c", "gg", sender="Dev", at=NOW - 800), msg("d", "ok", me=True, sender="Sam", at=NOW - 600)])
    old = chat(store, "Old friend")
    store.add_conversation(old, NOW - 10 * 86400, "focus", [msg("e", "you there?", sender="Ana", at=NOW - 10 * 86400)])
    page = store.upsert_thread("Google Chrome", "A page", "https://example.com/", process="chrome", kind="page")
    store.add_conversation(page, NOW - 400, "focus", [msg("f", "a comment", sender="Bo", at=NOW - 400)])
    got = reply_candidates(store, NOW)
    assert [c.thread_id for c in got] == [riya]
    assert got[0].name == "Riya" and got[0].newest.text == "did you get the photos?" and [m.text for m in got[0].messages] == ["hey", "did you get the photos?"]
    # your reply is captured: no longer a candidate
    store.add_conversation(riya, NOW - 60, "focus", [msg("g", "yes, thanks!", me=True, sender="Sam", at=NOW - 60)])
    assert reply_candidates(store, NOW) == []


def _board_setup(store):
    riya = chat(store, "Riya")
    store.add_conversation(riya, NOW - 600, "focus", [msg("a", "did you get the photos?", at=NOW - 700)])
    fam = chat(store, "Family", app="WhatsApp", process="whatsapp")
    store.add_conversation(fam, NOW - 500, "focus", [msg("b", "dinner at 8", sender="Mum", at=NOW - 900), msg("c", "ok", sender="Dad", at=NOW - 800)])
    return riya, fam


def test_the_check_judges_each_chat_once_and_the_board_shows_the_ones_waiting_on_you(store):
    from yuki.memory.workboard import WorkBoardChecker, board

    riya, fam = _board_setup(store)
    ids = {m.text: m.id for c in __import__("yuki.memory.workboard", fromlist=["x"]).reply_candidates(store, NOW) for m in c.messages}
    answer = {"chats": [{"chat": riya, "needs_reply": True, "who": "Riya", "quote": ids["did you get the photos?"], "place": "your DM"},
                        {"chat": fam, "needs_reply": False}], "loops": [], "meetings": []}
    worker = _Worker(store, answer)
    checker = WorkBoardChecker(worker)
    result = checker.check(NOW, force=True)
    assert result.ran and result.chats == 2 and result.replies == 1
    assert "C%d" % riya in worker.calls[0] and "The user's own names: Sam" in worker.calls[0]
    b = board(store, NOW)
    assert [r["key"] for r in b["replies"]] == [f"chat:{riya}"]
    r = b["replies"][0]
    assert r["who"] == "Riya" and r["msg"] == "did you get the photos?" and r["place"] == "your DM" and r["app"] == "Discord"
    assert r["opened"] is False and b["ready"] is True
    # judged once per newest message: nothing to do on the next check
    assert checker.pending(NOW + 1000)[0] == []
    assert not checker.due(NOW + 2000)


def test_a_quote_is_always_one_of_that_chats_own_messages_and_an_omitted_chat_is_not_asked_again(store):
    from yuki.memory.workboard import WorkBoardChecker, board, reply_candidates

    riya, fam = _board_setup(store)
    fam_msg = next(m.id for c in reply_candidates(store, NOW) if c.thread_id == fam for m in c.messages)
    answer = {"chats": [{"chat": riya, "needs_reply": True, "who": "", "quote": fam_msg, "place": "your DM"},
                        {"chat": 99999, "needs_reply": True}], "loops": [], "meetings": []}
    result = WorkBoardChecker(_Worker(store, answer)).check(NOW, force=True)
    assert any("C99999" in x for x in result.rejected)
    r = board(store, NOW)["replies"][0]
    assert r["msg"] == "did you get the photos?" and r["who"] == "Riya"      # their own newest, the sender's name
    row = store._query("SELECT basis FROM work_items WHERE key=?", (f"chat:{fam}",))
    assert row and row[0][0]                                                   # judged (left out): not asked again


def test_no_reply_needed_and_open_chat_marks_and_your_reply_clears_it(store):
    from yuki.memory.workboard import WorkBoardChecker, board, reply_candidates, set_mark

    riya, _fam = _board_setup(store)
    q = reply_candidates(store, NOW)[-1].newest.id if reply_candidates(store, NOW)[-1].thread_id == riya else None
    answer = {"chats": [{"chat": riya, "needs_reply": True, "who": "Riya", "quote": q, "place": "your DM"}], "loops": [], "meetings": []}
    checker = WorkBoardChecker(_Worker(store, answer))
    checker.check(NOW, force=True)
    key = f"chat:{riya}"
    assert set_mark(store, key, "opened")
    assert board(store, NOW)["replies"][0]["opened"] is True
    assert set_mark(store, key, "no_reply")
    assert board(store, NOW)["replies"] == []
    assert set_mark(store, key, None)                                          # Undo
    assert board(store, NOW)["replies"][0]["key"] == key
    assert set_mark(store, key, "no_reply")
    # a newer message from them: the dismissal lapses, but it shows again only once that message is judged
    store.add_conversation(riya, NOW + 60, "focus", [msg("z", "and the videos?", at=NOW + 60)])
    assert board(store, NOW + 70)["replies"] == []
    checker.check(NOW + 2000, force=True)
    assert board(store, NOW + 2000)["replies"][0]["key"] == key
    # your reply is captured: gone, no model needed
    store.add_conversation(riya, NOW + 2100, "focus", [msg("y", "sending now", me=True, sender="Sam", at=NOW + 2100)])
    assert board(store, NOW + 2200)["replies"] == []


def test_marks_are_closed_names_on_the_right_kind_of_row(store):
    from yuki.memory.workboard import set_mark

    riya, _ = _board_setup(store)
    assert not set_mark(store, f"chat:{riya}", "listed")
    assert not set_mark(store, "commit:3", "no_reply")
    assert not set_mark(store, "chat:abc", "opened")
    assert not set_mark(store, "commit:3", "sent")
    assert set_mark(store, "commit:3", "listed") and set_mark(store, "loop:4", "listed")
    from yuki.memory.workboard import board
    assert board(store, NOW)["listed"] == ["commit:3", "loop:4"]


def test_loops_get_a_kind_who_and_what_and_a_waiting_one_a_draft(store):
    from yuki.memory.workboard import WorkBoardChecker, board

    dm = chat(store, "Theo")
    store.add_conversation(dm, NOW - 86400, "focus", [msg("m1", "u coming?", me=True, sender="Sam", at=NOW - 86400),
                                                       msg("m2", "yes in 10", sender="Theo", at=NOW - 86300)])
    j = store.add_journal(NOW - 86300, dm, "Discord", "Theo said he would join the voice channel.", 5)
    w = store.add_open_loop("Theo", "Theo said he would join the voice channel (Theo on Discord on 2026-10-02).",
                            opened_at=NOW - 86300, source_ids=[j])
    p = store.add_open_loop("Asha", "The user will send Asha the invoice (the user promised Asha on WhatsApp on 2026-10-02).",
                            opened_at=NOW - 7200)
    answer = {"chats": [], "loops": [
        {"loop": w, "kind": "waiting", "who": "Theo", "what": "join the voice channel", "where": "Discord",
         "basis": "offered", "app": "Telegram", "nudge": "hey, still joining the call?"},
        {"loop": p, "kind": "promised", "who": "Asha", "what": "", "where": "WhatsApp"}], "meetings": []}
    worker = _Worker(store, answer)
    result = WorkBoardChecker(worker).check(NOW, force=True)
    assert "u coming?" in worker.calls[0]                                   # how you write to him, by provenance
    loops = board(store, NOW)["loops"]
    assert loops[w] == {"kind": "waiting", "who": "Theo", "what": "join the voice channel", "where": "Discord",
                        "draft": "message Theo on Discord: hey, still joining the call?", "basis": "offered"}
    assert loops[p]["kind"] == "other" and "draft" not in loops[p]              # promised without what: refused
    assert any(f"L{p}" in x for x in result.rejected)
    # judged per wording: unchanged, nothing to do; reworded, judged again
    checker = WorkBoardChecker(worker)
    assert checker.pending(NOW + 10)[1] == []
    store._write(lambda conn: conn.execute("UPDATE open_loops SET text_ciphertext=? WHERE id=?",
                                           (store.cipher.encrypt("Theo said he would join tomorrow (Discord)."), w)))
    assert [lp.id for lp in checker.pending(NOW + 10)[1]] == [w]


def test_a_nudge_is_built_from_the_loops_own_person_and_app_never_the_models_recipient(store):
    """Second review S5/M2: the model gives only the nudge's words; who must be named by the loop itself, and the app is
    memory's record of the loop's chat. A recipient or app the loop doesn't name never reaches the ask bar."""
    from yuki.memory.workboard import WorkBoardChecker, board

    lp = store.add_open_loop("Theo", "Theo said he would send the invented tickets (Theo on Discord).", opened_at=NOW - 3600)
    own = store.add_open_loop("grant deadline", "The user wants the grant form finished by Friday; Ola still has to send "
                              "the budget links.", opened_at=NOW - 3600)
    other = store.add_open_loop("Dev", "Dev will check the invented planner build.", opened_at=NOW - 3600)
    answer = {"chats": [], "meetings": [], "loops": [
        {"loop": lp, "kind": "waiting", "who": "mallory@evil.example", "what": "send the tickets", "app": "Gmail",
         "nudge": "here are the login codes"},
        {"loop": own, "kind": "promised", "who": "Priya", "what": "Finish the grant form"},
        {"loop": other, "kind": "waiting", "who": "Dev", "what": "check the build", "basis": "asked", "app": "Signal",
         "nudge": "any news on the build?"}]}
    result = WorkBoardChecker(_Worker(store, answer)).check(NOW, force=True)
    loops = board(store, NOW)["loops"]
    assert loops[lp]["kind"] == "other" and not loops[lp].get("draft")              # nobody the loop names: no nudge
    assert loops[own]["kind"] == "asked" and loops[own]["who"] == ""                # a promise to someone it never names
    assert loops[other]["draft"] == "message Dev on Signal: any news on the build?" and loops[other]["basis"] == "asked"
    assert sum("who not named in the loop" in x for x in result.rejected) == 2


def test_an_ask_to_talk_comes_with_the_calls_held_in_that_app_since(store, monkeypatch):
    """Second review M1: under a chat, the calls memory recorded in that same app after its first message shown (times
    only), so an ask to talk that a call answered is not left waiting; never for a browser. One chat captured as two
    threads shows once."""
    from yuki.memory.workboard import WorkBoardChecker, board, reply_candidates

    noor = chat(store, "Noor", app="Slack", process="slack")
    store.add_conversation(noor, NOW - 3000, "focus", [msg("n1", "got a minute for a quick call?", sender="Noor", at=NOW - 3000),
                                                        msg("n2", "You joined the huddle", sender="Noor", at=NOW - 2900)])
    web = chat(store, "Riya", app="Google Chrome", process="chrome")
    store.add_conversation(web, NOW - 2000, "focus", [msg("w1", "call later?", sender="Riya", at=NOW - 2000)])
    store.start_call(app="Slack", process="slack.exe", via="mic", at=NOW - 2880, status="ended")
    store.start_call(app="Google Chrome", process="chrome.exe", via="mic", at=NOW - 1900, status="ended")
    store.start_call(app="Slack", process="slack.exe", via="mic", at=NOW - 90000, status="ended")      # long before
    monkeypatch.setattr(store, "sites_shown", lambda process, since=None: 9 if "chrome" in str(process) else 1)
    worker = _Worker(store, {"chats": [], "loops": [], "meetings": []})
    checker = WorkBoardChecker(worker)
    user, _shown = checker.message(reply_candidates(store, NOW), [], False, NOW)
    lines = user.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith(f"- C{noor} "))
    assert lines[at + 3].startswith("  calls in this app since: ") and lines[at + 3].count(":") >= 2
    assert lines[at + 3].count(" min") + lines[at + 3].count("still on") == 1                  # only the one after
    wat = next(i for i, x in enumerate(lines) if x.startswith(f"- C{web} "))
    assert not lines[wat + 2].startswith("  calls in this app")                                   # a browser: none
    # the same chat captured twice (its name read two ways): one row on the page
    twin = chat(store, "Noor (DM)", app="Slack", process="slack")
    store.add_conversation(twin, NOW - 3000, "focus", [msg("t1", "got a minute for a quick call?", sender="Noor", at=NOW - 3000),
                                                        msg("t2", "You joined the huddle", sender="Noor", at=NOW - 2900)])
    ans = {"chats": [{"chat": c, "needs_reply": True, "who": "Noor", "place": "your DM"} for c in (noor, twin)],
           "loops": [], "meetings": []}
    store._write(lambda conn: conn.execute("DELETE FROM work_items"))
    WorkBoardChecker(_Worker(store, ans)).check(NOW, force=True)
    rows = board(store, NOW)["replies"]
    assert [r["msg"] for r in rows if r["app"] == "Slack"] == ["You joined the huddle"]


def test_a_day_with_no_hour_said_is_a_day_and_meetings_cite_j_numbers_only(store):
    """Second review M3: a day-only event never gets an invented midnight; the schema asks for J numbers only."""
    from yuki.memory.workboard import TOOL, WorkBoardChecker, board

    j = store.add_journal(NOW - 3600, None, "Firefox", "The seed swap is on Tuesday at the community hall.", 5)
    tue = (datetime.fromtimestamp(NOW).date() + timedelta(days=3)).strftime("%Y-%m-%d")
    answer = {"chats": [], "loops": [], "meetings": [
        {"when": f"{tue} 00:00", "time_said": False, "title": "Seed swap", "journal_ids": [j]}]}
    WorkBoardChecker(_Worker(store, answer)).check(NOW, force=True)
    m = board(store, NOW)["meetings"]
    assert len(m) == 1 and m[0]["all_day"] is True
    props = TOOL["input_schema"]["properties"]["meetings"]["items"]
    assert "time_said" in props["required"] and "J numbers only" in props["properties"]["journal_ids"]["description"]


def test_continue_reopens_pages_you_viewed_never_a_link_someone_shared(store):
    """Second review S2: a link someone pasted in a chat of the thread is not among Continue's places."""
    from yuki.memory.store import WorkOp

    now = time.time()
    [op] = store.apply_work([WorkOp("starts", None, title="Invented report", state="Writing it.", at=now, seen_at=now)])
    tid = op.result_id
    store.upsert_links([
        {"url": "https://docs.example/report", "how": "viewed", "at": now - 100, "until": now - 50, "title": "Report",
         "host": "docs.example", "app": "Firefox", "work_thread_id": tid},
        {"url": "https://evil.example/payload.exe", "how": "shared", "at": now - 40, "until": now - 30,
         "title": "payload", "host": "evil.example", "app": "Discord", "sender": "Mallory", "work_thread_id": tid}])
    ctx = MemoryClient(store).thread_continue(f"thread:{tid}")
    urls = [pl["url"] for pl in ctx["places"]]
    assert "https://docs.example/report" in urls and not any("evil.example" in (u or "") for u in urls)


def test_meetings_lie_ahead_within_a_week_and_rest_on_entries_shown(store):
    from yuki.memory.workboard import WorkBoardChecker, board

    j1 = store.add_journal(NOW - 3600, None, "Google Chrome", "The calendar shows a planning call tomorrow at 11:00.", 6)
    tomorrow = (datetime.fromtimestamp(NOW) + timedelta(days=1)).strftime("%Y-%m-%d")
    answer = {"chats": [], "loops": [], "meetings": [
        {"when": f"{tomorrow} 11:00", "title": "Planning call on Meet", "journal_ids": [j1]},
        {"when": "2026-09-01 10:00", "title": "Past thing", "journal_ids": [j1]},
        {"when": f"{tomorrow} 15:00", "title": "Made up", "journal_ids": [123456]},
        {"when": "2026-12-01", "title": "Too far", "journal_ids": [j1]},
        {"when": "soon", "title": "No time", "journal_ids": [j1]}]}
    result = WorkBoardChecker(_Worker(store, answer)).check(NOW, force=True)
    assert result.meetings == 1 and len(result.rejected) == 4
    m = board(store, NOW)["meetings"]
    assert [x["title"] for x in m] == ["Planning call on Meet"] and m[0]["seen"] == ["Google Chrome"] and not m[0]["all_day"]
    # read again only after a newer entry and an hour
    checker = WorkBoardChecker(_Worker(store, answer))
    assert checker.pending(NOW + 600)[2] is False
    store.add_journal(NOW + 700, None, "Slack", "something new", 3)
    assert checker.pending(NOW + 700)[2] is False and checker.pending(NOW + 3700)[2] is True
    # a meeting that has passed leaves the board
    assert board(store, NOW + 2 * 86400)["meetings"] == []


def test_a_dry_run_writes_nothing(store):
    from yuki.memory.workboard import WorkBoardChecker

    riya, _ = _board_setup(store)
    result = WorkBoardChecker(_Worker(store, {"chats": [{"chat": riya, "needs_reply": True, "who": "Riya"}], "loops": [], "meetings": []})).check(NOW, force=True, dry_run=True)
    assert result.ran and result.written["replies"] == 1
    assert store._query("SELECT count(*) FROM work_items")[0][0] == 0


def test_a_failed_call_is_logged_and_writes_nothing(store):
    from yuki.memory.workboard import WorkBoardChecker

    _board_setup(store)

    class Boom(_Worker):
        def _call(self, *a, **k):
            raise RuntimeError("offline")

    w = Boom(store, {})
    result = WorkBoardChecker(w).check(NOW, force=True)
    assert result.error and not result.ran and any(t == "conversation_error" for t, _ in w.logged)
    assert store._query("SELECT count(*) FROM work_items")[0][0] == 0


def test_put_it_back_reopens_todos_commitments_and_loops_within_their_rules(store):
    m = MemoryClient(store)
    t = m.add_todo("Water the invented plants")
    assert m.end_todo(t, "done") == t
    assert m.reopen_item(t) == t and store.todos(("open",))[0].text == "Water the invented plants"
    # an old one: only Put it back (any age)
    m.end_todo(t, "done")
    store._write(lambda conn: conn.execute("UPDATE todos SET done_at=done_at-7200"))
    assert m.reopen_item(t) is None and m.reopen_item(t, any_age=True) == t
    # a commitment you ended: Undo within 10 minutes; one memory closed itself: Not done yet
    c = store.add_conversation_fact("commitment", "The user will send the invented deck", owner="user", origin="model")
    ref = f"commit:{c}"
    assert m.end_todo(ref, "wrong") == ref
    assert m.reopen_item(ref) == ref and store.conversation_fact(c).status == "active"
    store.end_conversation_fact(c, "done", note="done, by Yuki's to-do check against later evidence (J1): sent it")
    assert m.reopen_item(ref) is None                                        # memory's close: not an Undo
    assert m.reopen_item(ref, any_age=True) == ref
    f = store.conversation_fact(c)
    assert f.status == "active" and f.end_note is None and f.valid_to is None
    # a loop you said is done: open again, its portrait fact current, the closing correction ended
    lp = store.add_open_loop("Dev", "Dev will send the invented photos (Dev on Signal).")
    assert m.end_todo(f"loop:{lp}", "done") == f"loop:{lp}"
    corr = store._query("SELECT count(*) FROM portrait_facts WHERE kind='correction' AND valid_to IS NULL")[0][0]
    assert corr == 1
    assert m.reopen_item(f"loop:{lp}") == f"loop:{lp}"
    assert [x.id for x in store.open_loops("open")] == [lp]
    assert store._query("SELECT count(*) FROM portrait_facts WHERE kind='correction' AND valid_to IS NULL")[0][0] == 0
    assert m.reopen_item("thread:4") is None and m.reopen_item("todo:x") is None
    # an old loop: not an Undo, but Put it back (any age) still reopens it (review F7)
    assert m.end_todo(f"loop:{lp}", "done") == f"loop:{lp}"
    store._write(lambda conn: conn.execute("UPDATE open_loops SET resolved_at=resolved_at-7200 WHERE id=?", (lp,)))
    assert m.reopen_item(f"loop:{lp}") is None and m.reopen_item(f"loop:{lp}", any_age=True) == f"loop:{lp}"
    assert [x.id for x in store.open_loops("open")] == [lp]


def test_a_chat_says_whether_its_app_is_a_browser_by_measure(store, monkeypatch):
    """Review F1: Open chat needs to know a chat lives in a browser (another tab on top is not the chat). Measured from
    the timeline as the call watcher measures it (many sites shown), never a list of app names."""
    from yuki.memory.workboard import chat_place

    t = store.upsert_thread("Google Chrome", "#design", "https://chat.example.com/c/design", process="chrome",
                            scope="#design", kind="conversation")
    monkeypatch.setattr(store, "sites_shown", lambda process, since=None: 12)
    p = chat_place(store, t)
    assert p["browser"] is True and p["url"] == "https://chat.example.com/c/design" and p["name"] == "#design"
    monkeypatch.setattr(store, "sites_shown", lambda process, since=None: 1)
    assert chat_place(store, t)["browser"] is False


def test_the_due_date_an_item_has_reads_back_as_set_due_takes_it(store):
    m = MemoryClient(store)
    t = m.add_todo("Invented deck")
    assert m.due_of(t) == ""
    m.set_due(t, "2026-10-05")
    assert m.due_of(t) == "2026-10-05"
    m.set_due(t, "2026-10-05 17:30")
    assert m.due_of(t) == "2026-10-05 17:30"
    c = f"commit:{store.add_conversation_fact('commitment', 'The user will print the invented flyers', owner='user', origin='model')}"
    m.set_due(c, "2026-10-06")
    assert m.due_of(c) == "2026-10-06"
    assert m.due_of("loop:3") is None and m.due_of("todo:999") is None


def test_adding_from_words_waits_a_bounded_time_and_writes_once(store, monkeypatch):
    """Review F2: a slow due read is given a few seconds at most; the to-do is then written once with the words whole."""
    m = MemoryClient(store)

    class Slow:
        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **req):
            time.sleep(1.0)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"text": "pay rent", "due": "2026-10-04"})])

    ref, due = m.add_todo_words("pay rent tomorrow", Slow(), wait_s=0.2)
    assert due is None and [x.text for x in store.todos(("open",))] == ["pay rent tomorrow"]
    time.sleep(1.0)
    assert len(store.todos(("open",))) == 1


def test_the_add_field_reads_a_due_date_from_your_words():
    from yuki.memory.workboard import read_due

    class Client:
        def __init__(self, data):
            self.data = data
            self.messages = SimpleNamespace(create=self.create)
            self.seen: list[dict] = []

        def create(self, **req):
            self.seen.append(req)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.data)])

    c = Client({"text": "send Jonas the slides", "due": "2026-10-05"})
    assert read_due(c, "send Jonas the slides by Monday", NOW) == ("send Jonas the slides", "2026-10-05")
    assert "Sat 2026-10-03" in c.seen[0]["messages"][0]["content"]
    assert read_due(Client({"text": "pay rent", "due": "2026-10-04 10:00"}), "pay rent tomorrow 10am", NOW) == ("pay rent", "2026-10-04 10:00")
    assert read_due(Client({"text": "buy", "due": ""}), "buy milk", NOW) == ("buy milk", None)        # no due: words whole
    assert read_due(Client({"text": "x" * 99, "due": "2026-10-05"}), "short", NOW) == ("short", None)  # not the user's words
    assert read_due(None, "  buy   milk ", NOW) == ("buy milk", None)

    class Down:
        messages = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert read_due(Down(), "buy milk tomorrow", NOW) == ("buy milk tomorrow", None)


def test_the_worker_runs_the_check_after_the_commitments(store):
    from yuki.memory import conversations

    src = Path(conversations.__file__).read_text(encoding="utf-8")
    assert "self.check_commitments(wall)\n                self.check_work_board(wall)" in src.replace("\r\n", "\n")


def test_work_page_stamps_follow_work_items_and_messages(store):
    from yuki.memory.api import change_stamps

    m = MemoryClient(store)
    s0 = change_stamps(m, ("work_items", "messages", "conversation_facts"))
    riya, _ = _board_setup(store)
    from yuki.memory.workboard import set_mark
    set_mark(store, f"chat:{riya}", "opened")
    s1 = change_stamps(m, ("work_items", "messages", "conversation_facts"))
    assert s1["work_items"] != s0["work_items"] and s1["messages"] != s0["messages"]
    c = store.add_conversation_fact("commitment", "x", owner="user", origin="model")
    store.end_conversation_fact(c, "done", note="the user marked it done")
    s2 = change_stamps(m, ("conversation_facts",))
    m.reopen_item(f"commit:{c}")
    assert change_stamps(m, ("conversation_facts",)) != s2


def test_promises_memory_saw_kept_show_a_day_and_yours_never(store):
    from yuki.memory.workboard import kept_promises, session_threads

    now = time.time()
    a = store.add_conversation_fact("commitment", "The user will send the invented deck", owner="user", origin="model", title="Send the deck")
    b = store.add_conversation_fact("commitment", "The user will call the invented bank", owner="user", origin="model")
    c = store.add_conversation_fact("commitment", "Yuki will remind the user", owner="yuki", origin="model")
    store.end_conversation_fact(a, "done", at=now - 600, note="done, by Yuki's to-do check against later evidence (J1): sent")
    store.end_conversation_fact(b, "done", at=now - 300, note="the user marked it done")
    store.end_conversation_fact(c, "done", at=now - 200, note="done, by Yuki's to-do check against later evidence (J2): did")
    kept = kept_promises(store, now)
    assert [k["ref"] for k in kept] == [f"commit:{a}"] and kept[0]["title"] == "Send the deck"
    assert kept_promises(store, now + 2 * 86400) == []
    # Worked on today: a stretch belongs to the thread most of its journal entries belong to
    assert session_threads(store, [(now - 3600, now)]) == [None]


def test_worked_on_today_groups_a_stretch_under_its_thread(store):
    from yuki.memory.store import WorkOp
    from yuki.memory.workboard import session_threads

    now = time.time()
    [op] = store.apply_work([WorkOp("starts", None, title="Invented garden planner", state="Drawing the beds.", at=now, seen_at=now)])
    tid = op.result_id
    js = [store.add_journal(now - 1800 + i * 60, None, "VS Code", f"Invented entry {i}", 4) for i in range(3)]
    other = store.add_journal(now - 1700, None, "Firefox", "Another invented entry", 4)
    store._write(lambda conn: conn.execute("UPDATE journal SET work_thread_id=? WHERE id IN (%s)" % ",".join("?" * len(js)), (tid, *js)))
    assert session_threads(store, [(now - 3600, now), (now - 7200, now - 3600)]) == [{"ref": f"thread:{tid}", "title": "Invented garden planner"}, None]
    assert other


def test_a_correction_can_move_a_due_date(store):
    from yuki.memory.workboard import read_correction

    class Client:
        def __init__(self, data):
            self.messages = SimpleNamespace(create=lambda **k: SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)]))

    assert read_correction(Client({"due": "2026-10-05", "rest": ""}), "Send the deck", "it's due Monday", NOW) == ("2026-10-05", "")
    assert read_correction(Client({"due": "", "rest": "Lena owns this"}), "x", "Lena owns this", NOW) == (None, "Lena owns this")
    assert read_correction(None, "x", " that was  last week ", NOW) == (None, "that was last week")
    m = MemoryClient(store)
    t = m.add_todo("Invented deck")
    assert m.set_due(t, "2026-10-05") == t and store.todos(("open",))[0].due_all_day
    assert m.set_due(t, "2026-10-05 17:30") == t and not store.todos(("open",))[0].due_all_day
    assert m.set_due(t, None) == t and store.todos(("open",))[0].due_at is None
    c = store.add_conversation_fact("commitment", "The user will print the invented flyers", owner="user", origin="model")
    assert m.set_due(f"commit:{c}", "2026-10-06") == f"commit:{c}" and store.conversation_fact(c).due_at
    assert m.set_due("loop:3", "2026-10-06") is None and m.set_due(t, "Monday") is None
