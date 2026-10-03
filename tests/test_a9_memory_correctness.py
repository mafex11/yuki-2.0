"""Revamp A9 (docs/research/revamp-2026-10-02/22-build-A6-A9.md): migration 27 merges chats split by a display-name
change and keys threads by the app's process; game and DAW microphone use is not a meeting; threads go paused and are
never dated in the future; the recheck step corrects the weekly review and the portrait from withdrawn entries.

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


# -- the recheck: the review and the portrait against current memory ---------------------------------------------


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


def _review(store: Store) -> Any:
    rid = store.start_weekly_review("2026-W39", T0 - 7 * 86400, T0 + 86400, "scheduled", "m", at=T0 + 86400)
    sections = {"about": "The week ran on Corvana and VALORANT, and a Google job offer that landed Sunday evening. "
                         "He also sorted music.", "time": "31h at the PC.", "focus": "", "done": "", "still_open": "",
                "suggestions": ["Rest more."]}
    from yuki.memory.review import compose_text
    from yuki.memory.store import ReviewStats

    store.finish_weekly_review(rid, outcome="ok", stats=ReviewStats(), text=compose_text(sections), sections=sections,
                               teaser="A Google offer and 31h of work.", numbers={"x": 1})
    store.set_review_nudge(rid, 30)
    store._write(lambda conn: conn.execute("UPDATE weekly_reviews SET finished_at=? WHERE id=?", (T0 + 86400, rid)))
    return store.weekly_review(rid)


class _WordsEmbedder:
    """A stand-in for the shared embedder: a hashed bag of a text's words (minus a few function words), so texts
    sharing words sit near each other. Deterministic, no model."""

    model_name = "stub-words"
    _SKIP = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "that", "they", "had", "just", "from", "per"}

    def embed(self, texts: list[str]) -> Any:
        import numpy as np

        out = np.zeros((len(texts), 512), dtype=np.float32)
        for i, text in enumerate(texts):
            word = ""
            for ch in (text or "").casefold() + " ":
                if ch.isalnum():
                    word += ch
                    continue
                if word and word not in self._SKIP:
                    out[i, sum(map(ord, word)) * 31 % 512] += 1.0
                word = ""
        return out


def _stub_review_client(edits: list[dict[str, Any]], seen: list[dict[str, Any]] | None = None) -> Any:
    def create(**request: Any) -> Any:
        if seen is not None:
            seen.append(request)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name="correct_review", input={"edits": edits})],
            stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                          cache_creation_input_tokens=0, cache_read_input_tokens=0))

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_the_recheck_corrects_whole_sentences_and_stores_a_new_version(store):
    from yuki.memory.recheck import Rechecker, whole_sentence

    review = _review(store)
    tid = store.upsert_thread("Chrome", "Yuki voice samples", "file:///index.html", process="chrome.exe")
    demo = store.add_journal(T0 - 86400, tid, "Chrome", "The user told Yuki they got a job offer from Google.", 7)
    store._write(lambda conn: conn.execute("UPDATE journal SET superseded_at=?, superseded_by=12345 WHERE id=?",
                                           (T0 + 86400 + 3600, demo)))   # re-journaled after the review
    offer = {"words": "a Google job offer that landed Sunday evening", "journal_ids": [demo], "still_supported_by": []}
    fragment = {"section": "about", "sentence": "a Google job offer that landed Sunday evening", "replacement": "",
                "removed": [offer], "why": "demo text"}
    whole = {"section": "about",
             "sentence": "The week ran on Corvana and VALORANT, and a Google job offer that landed Sunday evening.",
             "replacement": "The week ran on Corvana and VALORANT.", "removed": [offer], "why": "demo text"}
    teaser = {"section": "teaser", "sentence": "A Google offer and 31h of work.", "replacement": "31h of work.",
              "removed": [{"words": "A Google offer and", "journal_ids": [demo], "still_supported_by": []}],
              "why": "demo text"}
    uncited = {"section": "about", "sentence": "He also sorted music.", "replacement": "",
               "removed": [{"words": "He also sorted music.", "journal_ids": [], "still_supported_by": []}],
               "why": "x"}
    client = _stub_review_client([fragment, whole, teaser, uncited])
    checker = Rechecker(store, client=client, log_dir=Path(tempfile.mkdtemp()), embedder=_WordsEmbedder())
    result = checker.run(render="skip", out=lambda *a: None)
    [found] = result.reviews
    assert [e["section"] for e in found["edits"]] == ["about", "teaser"] and len(found["rejected"]) == 2
    latest = store.weekly_reviews(week="2026-W39")[0]
    assert latest.id == found["new_id"] and latest.id != review.id
    assert "Google" not in latest.text and "He also sorted music." in latest.text and latest.teaser == "31h of work."
    assert latest.nudge_id == 30 and latest.at == review.at            # no new run, no new teaser card
    assert store.weekly_review(review.id).outcome == "superseded"
    assert not whole_sentence("a b. c d e.", "c d") and whole_sentence("a b. c d e.", "c d e.")
    # nothing withdrawn since the new version was written: nothing to do the next time
    assert checker.recheck_review(latest) is None


def test_the_recheck_keeps_an_open_todo_that_a_withdrawn_entry_only_touches(store):
    """Regression (live dry run, real memory, 3 Oct 2026, docs/research/revamp-2026-10-02/23 L7): besides the invented
    Google offer, the recheck also dropped "laminating the certificate documents for Bro" from the review's open items,
    citing a withdrawn "entrance exam score card" search - while the to-do was still open and sourced ("per Bro's request").
    The model now sees what is still true, names each claim it takes out, and code corrects a sentence only when
    every claim removed rests solely on withdrawn entries. Synthetic data, the real model's edits replayed."""
    from yuki.memory.recheck import Rechecker

    rid = store.start_weekly_review("2026-W39", T0 - 7 * 86400, T0 + 86400, "scheduled", "m", at=T0 + 86400)
    about = ("The week ran on the agent harness revamp and VALORANT sessions, and a Google job offer that landed "
             "Sunday evening.")
    still_open = ("Nothing is overdue. Two loops remain open: laminating the certificate documents for Bro, and "
                  "building the MCP server.")
    done = "28 of 31 to-dos closed, including Hemanth's Discord ping."
    sections = {"about": about, "time": "31h at the PC.", "focus": "", "done": done, "still_open": still_open,
                "suggestions": []}
    from yuki.memory.review import compose_text
    from yuki.memory.store import ReviewStats

    store.finish_weekly_review(rid, outcome="ok", stats=ReviewStats(), text=compose_text(sections), sections=sections,
                               teaser="31h on the clock.", numbers={"x": 1})
    store._write(lambda conn: conn.execute("UPDATE weekly_reviews SET finished_at=? WHERE id=?", (T0 + 86400, rid)))
    review = store.weekly_review(rid)
    tid = store.upsert_thread("Google Chrome", "Google", "https://www.google.com/", process="chrome.exe")
    offer = store.add_journal(T0 - 3600, tid, "Google Chrome",
                              "The user told Yuki they had just received a job offer from Google.", 7)
    search = store.add_journal(T0 - 3500, tid, "Google Chrome", "The user searched for '2022 entrance exam score card' on Google.", 4)
    for jid in (offer, search):   # both re-journaled after the review was written
        store._write(lambda conn, j=jid: conn.execute("UPDATE journal SET superseded_at=?, superseded_by=4242 WHERE id=?",
                                                      (T0 + 86400 + 3600, j)))
    store.add_open_loop("Bro", "The user needs to laminate the certificate documents sent to Bro, per Bro's request.",
                        opened_at=T0 - 9 * 86400)
    # what the model returned on the real memory (its "why" even said the certificate was not from a withdrawn entry)
    google = {"section": "about", "sentence": about,
              "replacement": "The week ran on the agent harness revamp and VALORANT sessions.",
              "removed": [{"words": "a Google job offer that landed Sunday evening", "journal_ids": [offer],
                           "still_supported_by": []}], "why": "Job offer entry was withdrawn"}
    bro = {"section": "still_open", "sentence": "Two loops remain open: laminating the certificate documents for Bro, "
                                                "and building the MCP server.",
           "replacement": "One loop remains open: building the MCP server.",
           "removed": [{"words": "laminating the certificate documents for Bro", "journal_ids": [search],
                        "still_supported_by": []}],
           "why": "Certificate task was about searching for own entrance exam score card"}
    noop = {"section": "done", "sentence": done, "replacement": done,
            "removed": [{"words": "Hemanth's Discord ping", "journal_ids": [offer], "still_supported_by": []}],
            "why": "no change needed"}
    seen: list[dict[str, Any]] = []
    checker = Rechecker(store, client=_stub_review_client([google, bro, noop], seen),
                        log_dir=Path(tempfile.mkdtemp()), embedder=_WordsEmbedder())
    found = checker.recheck_review(review, dry_run=True)
    # the model was shown the open to-do as current evidence
    prompt = seen[0]["messages"][0]["content"]
    assert "STILL TRUE NOW" in prompt and "laminate the certificate documents sent to Bro" in prompt
    assert [e["section"] for e in found["edits"]] == ["about"]
    assert found["edits"][0]["removed"] == ["a Google job offer that landed Sunday evening"]
    assert any(r.startswith("still_open:") and "does not rest on the withdrawn entries" in r for r in found["rejected"])
    assert any(r.startswith("done:") and "changes nothing" in r for r in found["rejected"])

    # the same edit with the model's own word that a current item supports it, and with a real citation: still kept
    bro_cited = dict(bro, removed=[{"words": "laminating the certificate documents for Bro",
                                    "journal_ids": [search, offer], "still_supported_by": [1]}])
    checker._client = _stub_review_client([bro_cited])
    assert "still supported by current memory (T1)" in checker.recheck_review(review, dry_run=True)["rejected"][0]
    # measured even when the model says nothing: a claim close to an open loop stays
    bro_quiet = dict(bro, removed=[{"words": "laminating the certificate documents for Bro",
                                    "journal_ids": [search], "still_supported_by": []}])
    _applied, rejected, _s, _t = checker.apply_edits(
        review, [bro_quiet], {search}, withdrawn={search: "The user laminated the certificate documents for Bro."},
        current=[{"id": "loop:1", "text": "The user needs to laminate the certificate documents sent to Bro, per "
                                          "Bro's request.", "evidence": ""}])
    assert rejected and "still supported by current memory (T1" in rejected[0]
    # a correction that takes out more than the claims it names is refused
    greedy = dict(google, replacement="The week ran on VALORANT.")
    _a, rejected, _s, _t = checker.apply_edits(review, [greedy], {offer}, withdrawn={offer: "The user told Yuki they "
                                               "had just received a job offer from Google."}, current=[])
    assert rejected and "more than the claims it names" in rejected[0]
    # without the embedder's vectors nothing is corrected
    checker._embedder = SimpleNamespace(embed=lambda texts: None, model_name="none")
    _a, rejected, _s, _t = checker.apply_edits(review, [google], {offer}, withdrawn={offer: "x"}, current=[])
    assert rejected and "could not be measured" in rejected[0]
    # applied for real, the review keeps Bro's to-do and loses the Google offer
    checker._embedder = _WordsEmbedder()
    checker._client = _stub_review_client([google, bro, noop])
    stored = checker.recheck_review(review)
    latest = store.weekly_review(stored["new_id"])
    assert "Google" not in latest.text and "laminating the certificate documents for Bro" in latest.text
