"""Revamp A9 (the design notes): migration 27 merges chats split by a display-name
change and keys threads by the app's process; game and DAW microphone use is not a meeting; threads go paused and are
never dated in the future; a withdrawn entry takes the portrait facts that rest on it out.

Temp databases, stub models and the stubbed timeline recorder; nothing touches the desktop or a real model.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yuki.memory.store as st
from yuki.memory.store import FactChange, Store, TimelineRow, WorkOp

T0 = datetime(2026, 10, 1, 21, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


# -- migration 27: one chat, one thread ---------------------------------------------------------------------------


def test_migration_27_merges_a_chat_split_by_the_apps_new_display_name(tmp_path, monkeypatch):
    """2 Oct: Windows renamed whatsapp.root "WhatsApp.Root" -> "WhatsApp": Hemanth (5 / 1106) and RES split in two."""
    path = tmp_path / "memory.db"
    monkeypatch.setattr(st, "MIGRATIONS", st.MIGRATIONS[:26])
    monkeypatch.setattr(st, "PY_MIGRATIONS", {})
    old = Store.open(path)
    assert old._query("PRAGMA user_version")[0][0] == 26

    def thread(app: str, first: float, last: float) -> int:
        key = old.cipher.digest("scope", app, "Hemanth")   # the old key: the display name
        return old._write(lambda conn: int(conn.execute(
            "INSERT INTO threads(thread_key, app, host, title_ciphertext, url_ciphertext, first_seen, last_seen,"
            " process, scope_ciphertext, kind) VALUES (?,?,NULL,?,NULL,?,?,?,?,?)",
            (key, app, old.cipher.encrypt(f"Hemanth - {app}"), first, last, "whatsapp.root",
             old.cipher.encrypt("Hemanth"), "conversation")).lastrowid))

    a = thread("WhatsApp.Root", T0, T0 + 3600)
    b = thread("WhatsApp", T0 + 86400, T0 + 90000)
    for tid, at in ((a, T0 + 60), (b, T0 + 86460)):
        old.add_capture(tid, at, "focus", f"bhai kab aayega {tid}", f"bhai kab aayega {tid}", kind="conversation")
        old.add_journal(at, tid, "WhatsApp", f"fact from {tid}", 5)
    for tid, fp in ((a, "f1"), (a, "same"), (b, "same"), (b, "f2")):
        old._write(lambda conn, tid=tid, fp=fp: conn.execute(
            "INSERT INTO messages(thread_id, fingerprint, content_key, first_seen, status) VALUES (?,?,?,?,'new')",
            (tid, fp, fp, T0)))
    other = old._write(lambda conn: int(conn.execute(
        "INSERT INTO threads(thread_key, app, title_ciphertext, first_seen, last_seen, process, scope_ciphertext)"
        " VALUES (?,?,?,?,?,?,?)", (old.cipher.digest("scope", "Discord", "#general"), "Discord",
                                   old.cipher.encrypt("#general - Discord"), T0, T0, "discord",
                                   old.cipher.encrypt("#general"))).lastrowid))
    old.close()
    monkeypatch.undo()

    new = Store.open(path)
    try:
        assert new._query("PRAGMA user_version")[0][0] == len(st.MIGRATIONS)
        rows = {r["id"]: r for r in new._query("SELECT id, merged_into, ident_key, first_seen, last_seen FROM threads")}
        assert rows[b]["merged_into"] == a and rows[a]["merged_into"] is None and rows[b]["ident_key"] is None
        assert rows[a]["first_seen"] == T0 and rows[a]["last_seen"] == T0 + 90000
        assert new.thread_info(a).title == "Hemanth - WhatsApp"            # the newest title stays
        assert new._query("SELECT count(*) FROM captures WHERE thread_id=?", (a,))[0][0] == 2
        assert new._query("SELECT count(*) FROM journal WHERE thread_id=?", (a,))[0][0] == 2
        assert sorted(r[0] for r in new._query("SELECT fingerprint FROM messages WHERE thread_id=?", (a,))) == [
            "f1", "f2", "same"]                                            # the message both had, once
        assert new._query("SELECT status FROM messages WHERE thread_id=?", (b,))[0][0] == "reread"
        assert rows[other]["merged_into"] is None and rows[other]["ident_key"]
        # from now on the chat is one thread, whatever name Windows gives the app
        for app in ("WhatsApp", "WhatsApp.Root", "WhatsApp Desktop"):
            assert new.upsert_thread(app, "Hemanth", None, process="WhatsApp.Root.exe", scope="Hemanth") == a
            assert new.find_thread(app, "Hemanth", None, process="WhatsApp.Root.exe", scope="Hemanth") == a
        fresh = new.upsert_thread("WhatsApp", "Bro", None, process="WhatsApp.Root.exe", scope="Bro")
        assert fresh not in (a, b) and new.upsert_thread("WhatsApp.Root", "Bro", None, process="whatsapp.root.exe",
                                                         scope="Bro") == fresh
    finally:
        new.close()


# -- meetings: a game or a DAW holding the microphone is not one --------------------------------------------------


def _row(store: Store, start: float, end: float, process: str, app: str, meeting: str | None,
         host: str | None = None) -> None:
    store.save_timeline(TimelineRow(None, start, end, "present", process, app, host=host, active_s=end - start,
                                    meeting=meeting))


def test_meeting_rows_without_a_call_of_their_app_read_as_that_apps_time(store):
    """2 Oct: VALORANT's voice chat and FL Studio recording were 1 h 35 m of "meetings" and held back cards."""
    early = T0 - 5 * 86400
    _row(store, early, early + 600, "zoom", "Zoom", "Zoom")                  # before any call was ever recorded
    first = store.start_call(app="Discord", process="discord", via="screen", at=T0 - 86400)
    store.update_call(first, ended_at=T0 - 86000)
    meet = store.start_call(app="Google Chrome", process="chrome", via="screen", at=T0 + 30)
    store.update_call(meet, ended_at=T0 + 3600)
    _row(store, T0, T0 + 3500, "chrome", "Google Chrome", "Google Meet")    # a call of its app around it
    _row(store, T0 + 4000, T0 + 6000, "fl64", "FL Studio", "FL Studio")     # recording a vocal: no call
    _row(store, T0 + 7000, T0 + 9000, "valorant", "VALORANT", "VALORANT")   # in-game voice: no call
    # a site that took the microphone in a browser is that site's meeting (the call judge once missed a Zen Meet)
    _row(store, T0 + 9500, T0 + 9900, "zen", "Zen", "Google Meet", host="meet.google.com")
    # rows recorded since the live gate (migration 27) were judged on a screen already: read as they are
    assert {r.app: r.meeting for r in store.timeline_between(T0 - 1, T0 + 10_000)}["FL Studio"] == "FL Studio"
    store.set_source_checkpoint(st.MEETING_GATE_CHECKPOINT, str(time.time() + 60))   # these rows came before it
    store._meeting_gate_since = None
    got = {r.app: r.meeting for r in store.timeline_between(early - 1, T0 + 10_000)}
    assert got == {"Zoom": "Zoom", "Google Chrome": "Google Meet", "FL Studio": None, "VALORANT": None,
                   "Zen": "Google Meet"}
    from yuki.memory.timeline import aggregate

    agg = aggregate(store.timeline_between(T0 - 1, T0 + 10_000), T0 - 1, T0 + 10_000, "app")
    assert {m["label"] for m in agg["meetings"]} == {"Google Meet"}     # the Chrome call and the Zen site
    assert not any(m["label"] in ("FL Studio", "VALORANT") for m in agg["meetings"])


class _Evidence:
    """The call worker's screen verdicts, stubbed: in a call or not, for every holder."""

    def __init__(self, in_call: bool) -> None:
        self.in_call = in_call

    def state(self, holder: Any, since: float, now: float | None = None) -> tuple[str | None, str]:
        return ("fresh", "Discord") if self.in_call else (None, "")


def test_a_hold_is_a_meeting_only_on_a_screen_judged_in_a_call(store, tmp_path):
    from test_av_signals import Stubs, _facts, _recorder

    fl = r"C:\Program Files\Image-Line\FL Studio 2024\FL64.exe"
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, fl, "FL Studio"), "Untitled - FL Studio")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 10\n")
    rec._evidence = _Evidence(False)
    rec._input_at = t0
    stubs.hold("mic", fl, t0 + 1)
    rec._open(1, t0)
    for dt in (15, 30, 60, 300):
        rec._poll_av(t0 + dt)
        rec._check_meeting(t0 + dt)
    assert rec._current.meeting is None and rec._current.title == "Untitled - FL Studio"
    assert rec.counters["mic_no_call"] == 1                # logged once for the hold
    rec._evidence = _Evidence(True)                        # the judge saw a call on its screen
    rec._poll_av(t0 + 320)
    rec._check_meeting(t0 + 320)
    assert rec._current.meeting == "FL Studio"


# -- threads of work: paused by measured inactivity, never dated in the future ------------------------------------


def test_a_thread_is_never_dated_after_it_was_seen_nor_before_memory_began(store):
    seen = T0
    tid = store.upsert_thread("Explorer", "Music", None, process="explorer.exe")
    store.add_capture(tid, T0 - 86400, "focus", "x", "x", kind="list")   # memory began a day before
    future = datetime(2026, 11, 8).timestamp()            # a calendar row printed "08 Nov"
    long_ago = datetime(2024, 11, 8).timestamp()          # a file listed with its 2024 date
    store.apply_work([WorkOp("starts", None, title="Music folder sorting", at=future, seen_at=seen),
                      WorkOp("starts", None, title="Beat archive", at=long_ago, seen_at=seen)])
    rows = {t.title: t for t in store.work_threads(("active",))}
    assert rows["Music folder sorting"].first_seen <= seen + 60 and rows["Music folder sorting"].last_seen <= seen + 60
    assert rows["Beat archive"].first_seen == seen
    # rows written before the fix are set right from their own events
    bad = rows["Beat archive"].id
    store._write(lambda conn: conn.execute("UPDATE work_threads SET first_seen=?, last_seen=? WHERE id=?",
                                           (long_ago, future, bad)))
    store._write(lambda conn: conn.execute("UPDATE work_thread_events SET at=?, created_at=? WHERE work_thread_id=?",
                                           (long_ago, seen + 120, bad)))   # written ~2 min after it was seen
    changed = store.repair_work_thread_dates(now=T0 + 3600)
    fixed = store.work_thread(bad)
    assert [c["id"] for c in changed] == [bad] and fixed.first_seen == seen + 120 and fixed.last_seen <= T0 + 3600


def test_the_nightly_tidy_pauses_long_quiet_threads_by_measure(store):
    from yuki.memory.workthreads import AUTO_PAUSE_DAYS, Consolidator

    now = time.time()
    store.apply_work([WorkOp("starts", None, title="Quiet project", at=now - (AUTO_PAUSE_DAYS + 1) * 86400),
                      WorkOp("starts", None, title="A few days quiet", at=now - 4 * 86400),
                      WorkOp("starts", None, title="Today's work", at=now - 3600)])
    sent: list[str] = []

    def call(**kw: Any) -> dict[str, Any]:
        sent.append(kw["user"])
        return {"decisions": [], "questions": []}

    worker = SimpleNamespace(store=store, model="m", effort="low", embedder=None, _call=call,
                             _log=lambda *a, **k: None, _seal=lambda v: "sealed")
    result = Consolidator(worker).run(now=now)
    status = {t.title: t.status for t in store.work_threads(("active", "paused"))}
    assert status == {"Quiet project": "paused", "A few days quiet": "active", "Today's work": "active"}
    assert "idle 4.0 days" in sent[0] and any("untouched for" in d for d in result.diary)
    # the model is told the measured rule and decides the quieter ones
    from yuki.memory.workthreads import CONSOLIDATE_SYSTEM_PROMPT

    assert "{pause_days}" in CONSOLIDATE_SYSTEM_PROMPT


# -- withdrawn entries: the portrait against current memory -------------------------------------------------------


def test_withdrawn_means_tainted_or_rejournaled_into_nothing(store):
    tid = store.upsert_thread("Chrome", "Yuki voice samples", "file:///index.html", process="chrome.exe")
    demo = store.add_journal(T0, tid, "Chrome", "The user said they got a job offer from Google.", 7)
    fixed = store.add_journal(T0, tid, "Chrome", "The user said they would push the release.", 6)
    replacement = store.add_journal(T0, tid, "Chrome", "The page said the release was pushed.", 6, batch_id=None)
    sample = store.add_journal(T0, tid, "Chrome", "A sample chat line.", 4)
    store._write(lambda conn: conn.execute("INSERT INTO journal_batches(id, at, thread_id, model, capture_count,"
                                           " input_chars, outcome) VALUES (777, ?, ?, 'm', 1, 1, 'ok')", (T0, tid)))
    store._write(lambda conn: conn.execute("INSERT INTO journal_batches(id, at, thread_id, model, capture_count,"
                                           " input_chars, outcome) VALUES (778, ?, ?, 'm', 1, 1, 'ok')", (T0, tid)))
    store._write(lambda conn: conn.execute("UPDATE journal SET batch_id=778 WHERE id=?", (replacement,)))
    store._write(lambda conn: conn.execute("UPDATE journal SET superseded_at=?, superseded_by=777 WHERE id=?",
                                           (T0 + 10, demo)))
    store._write(lambda conn: conn.execute("UPDATE journal SET superseded_at=?, superseded_by=778 WHERE id=?",
                                           (T0 + 10, fixed)))
    store._write(lambda conn: conn.execute("UPDATE journal SET tainted=1 WHERE id=?", (sample,)))
    assert store.withdrawn_journal([demo, fixed, replacement, sample, 999999]) == {demo, sample}
    from yuki.memory.portrait import withdrawn_facts

    facts = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "offer", "Got a job offer from Google.", 0.6, [demo], T0),
        FactChange("ADD", None, "work", "release", "Pushes releases.", 0.8, [fixed], T0),
        FactChange("ADD", None, "work", "user's", "Works at Corvana.", 1.0, [demo], T0, origin="user")])
    ids = [f for _ok, f in facts]
    assert withdrawn_facts(store, store.portrait_facts()) == {ids[0]}   # never the user's own
