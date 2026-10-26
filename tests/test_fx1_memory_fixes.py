"""Review 34 fixes, builder fx1, memory side (the design notes, 35-build-review-fixes.md).

H8 the thread-date repair's floor is when memory began (never the oldest capture the 30-day prune left), M5 an empty
people index falls back to the portrait, M9 withdrawn_journal is indexed, short-circuited and cached, M10 re-read
messages are not counted, M11 the People list reads shared things once, caches route measures and pauses while the
window is minimised.

Temp databases, invented names and stub memories only: no desktop, no model, no real memory.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.store import Store, WorkOp

DAY = 86400.0


@pytest.fixture()
def store(tmp_path: Path):
    s = Store.open(tmp_path / "memory.db")
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# H8: the floor of the thread-date repair
# ---------------------------------------------------------------------------


def _event(store: Store, wid: int, at: float) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO work_thread_events(work_thread_id, at, action, batch_id, thread_id, app, host, state_ciphertext,"
        " origin, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (wid, at, "touched", None, None, "Code", None, None,
                                                             "journal", at + 120)))


def test_h8_a_true_start_older_than_the_capture_ttl_is_never_rewritten(tmp_path: Path) -> None:
    """Reproduction of review 34 H8: memory began 60 days ago, captures are pruned at 30; a thread that truly started
    45 days ago and was touched this week kept being moved to its first event inside the last 31 days."""
    path = tmp_path / "memory.db"
    now = time.time()
    began = now - 60 * DAY
    s = Store.open(path)
    try:
        tid = s.upsert_thread("Code", "budget.py", None, process="code.exe")
        s._write(lambda conn: conn.execute("UPDATE threads SET first_seen=? WHERE id=?", (began, tid)))
        s.add_capture(tid, began + 60, "focus", "a", "a", kind="document")
        s.add_capture(tid, now - 3600, "focus", "b", "b", kind="document")
        s.apply_work([WorkOp("starts", None, title="Budget model", at=now - 45 * DAY, seen_at=now - 45 * DAY)])
        work = s.work_threads(("active",))[0]
        _event(s, work.id, now - 2 * DAY)
        assert s.prune(30) == 1                                    # the oldest capture is gone
    finally:
        s.close()
    s = Store.open(path)                                           # a new process: nothing cached
    try:
        oldest_capture = s._query("SELECT min(at) FROM captures")[0][0]
        first = s.work_thread(work.id).first_seen
        assert first == pytest.approx(now - 45 * DAY, abs=61)
        assert first < oldest_capture - DAY                        # what the old floor (min captures.at) rewrote
        assert s.memory_started_at() == pytest.approx(began, abs=1)
        assert s.repair_work_thread_dates(now=now, dry_run=True) == []
        assert s.repair_work_thread_dates(now=now) == []
        assert s.work_thread(work.id).first_seen == pytest.approx(first)
        # impossible and future dates are still set right, and a dry run writes nothing
        long_ago, future = now - 400 * DAY, now + 30 * DAY
        s._write(lambda conn: conn.execute("UPDATE work_threads SET first_seen=?, last_seen=? WHERE id=?",
                                           (long_ago, future, work.id)))
        planned = s.repair_work_thread_dates(now=now, dry_run=True)
        assert [c["id"] for c in planned] == [work.id]
        assert s.work_thread(work.id).first_seen == pytest.approx(long_ago)    # dry run: unchanged
        changed = s.repair_work_thread_dates(now=now)
        fixed = s.work_thread(work.id)
        assert [c["id"] for c in changed] == [work.id]
        assert fixed.first_seen >= began - DAY and fixed.last_seen <= now + 60
    finally:
        s.close()


def test_h8_the_floor_never_comes_from_content_dates(store: Store) -> None:
    now = time.time()
    tid = store.upsert_thread("WhatsApp", "Sam", None, process="whatsapp.exe")
    store.add_journal(now - 5 * 365 * DAY, tid, "WhatsApp", "An old message read back from 2021.", 3)
    started = store.memory_started_at()
    assert started is not None and started > now - DAY             # the thread's first sight, not the 2021 date


# ---------------------------------------------------------------------------
# M9: withdrawn_journal
# ---------------------------------------------------------------------------


def _journal_world(store: Store, n_batches: int = 400, per_batch: int = 50) -> dict[str, Any]:
    """Batches of journal rows: every 4th batch was re-journaled by a later one that wrote a standing replacement for
    the same capture thread, every 8th by one that wrote nothing standing; a few entries are tainted."""
    now = time.time()

    def op(conn: Any) -> dict[str, Any]:
        conn.executemany("INSERT INTO threads(id, thread_key, app, title_ciphertext, first_seen, last_seen)"
                         " VALUES (?,?,?,?,?,?)", [(t, f"k{t}", "App", "x", now, now) for t in range(1, 91)])
        rows, batches = [], []
        for b in range(1, n_batches * 2 + 1):
            batches.append((b, now, "m", 0, 0, 0, "ok"))
        conn.executemany("INSERT INTO journal_batches(id, at, model, capture_count, input_chars, facts, outcome)"
                         " VALUES (?,?,?,?,?,?,?)", batches)
        jid = 0
        expect: set[int] = set()
        for b in range(1, n_batches + 1):
            for k in range(per_batch):
                jid += 1
                thread = (b * 7 + k) % 90 + 1
                tainted = int(jid % 97 == 0)
                superseded_by = None
                if b % 4 == 0:
                    superseded_by = n_batches + b                      # replaced by a batch that wrote a replacement
                elif b % 8 == 2:
                    superseded_by = n_batches + b                      # replaced by a batch that wrote nothing
                rows.append((jid, now - jid, thread, "App", "x", 3, b, now, now if superseded_by else None,
                             superseded_by, tainted))
                if tainted or (superseded_by and b % 4 != 0):
                    expect.add(jid)
            if b % 4 == 0:                                             # the replacement rows, one per thread used
                for thread in {(b * 7 + k) % 90 + 1 for k in range(per_batch)}:
                    jid += 1
                    rows.append((jid, now, thread, "App", "x", 3, n_batches + b, now, None, None, 0))
        conn.executemany("INSERT INTO journal(id, at, thread_id, app, fact_ciphertext, importance, batch_id,"
                         " created_at, superseded_at, superseded_by, tainted) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return {"expect": expect, "last": jid, "n_batches": n_batches}

    return store._write(op)


def test_m9_withdrawn_journal_uses_the_index_and_is_right_and_fast(store: Store) -> None:
    w = _journal_world(store)
    plan = " ".join(str(r[-1]) for r in store._query(
        "EXPLAIN QUERY PLAN SELECT 1 FROM journal n WHERE n.batch_id = ? AND n.thread_id IS ? AND n.superseded_at IS"
        " NULL AND coalesce(n.tainted, 0) = 0", (5, 3)))
    assert "journal_batch_thread" in plan, plan                       # migration 32's index
    ids = list(range(1, w["last"] + 1, 13))                            # ~1,600 ids against ~23,000 rows
    started = time.perf_counter()
    gone = store.withdrawn_journal(ids)
    elapsed = time.perf_counter() - started
    assert gone == {i for i in ids if i in w["expect"]}
    assert elapsed < 1.0, elapsed


def test_m9_answers_are_cached_per_entry_and_a_rejournal_asks_again(store: Store, monkeypatch) -> None:
    w = _journal_world(store, n_batches=16, per_batch=5)
    ids = list(range(1, w["last"] + 1))
    first = store.withdrawn_journal(ids)
    asked: list[str] = []
    real = store._query
    monkeypatch.setattr(store, "_query", lambda sql, *a, **k: (asked.append(sql), real(sql, *a, **k))[1])
    assert store.withdrawn_journal(ids) == first and not asked          # every answer came from the cache
    # a re-journal of batch 1 that writes nothing standing: its entries are withdrawn from now on
    store._write(lambda conn: store._supersede_batches(conn, 30, [1]))
    again = store.withdrawn_journal(ids)
    assert asked and again == first | {1, 2, 3, 4, 5}
    asked.clear()
    assert store.withdrawn_journal(list(first)) == first and not asked   # withdrawn stays withdrawn, no query


# ---------------------------------------------------------------------------
# M10 / M11: contact counts and the People list
# ---------------------------------------------------------------------------


def _reread(store: Store, thread_id: int, n: int, *, me: bool, at: float) -> None:
    """A message seen again under a new time label (the watcher's ``reread``: no sender, no text kept)."""
    store._write(lambda conn: conn.execute(
        "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label,"
        " at, text_ciphertext, first_seen, status, journaled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, None, store.cipher.digest("fp-reread", str(thread_id), str(n)), store.cipher.digest("ck", str(n)),
         None, int(me), "Yesterday", at, None, at, "reread", 1)))


def _numbers(store: Store, now: float) -> dict[str, Any]:
    from yuki.memory import contacts as C

    out: dict[str, Any] = {}
    for person in C.people(store):
        card = C.card(store, person, now=now, with_samples=0)
        out[person.name] = [(r["label"], r["mine"], r["theirs"]) for r in card["routes"]]
    rhythm = C.rhythm(store, C.people(store), now=now)
    out["rhythm"] = {k: (v["mine"], v["theirs"]) for k, v in rhythm.items()}
    g = C.gather(store, now=now)
    out["gather"] = {t.id: (t.mine, t.theirs) for t in g.threads.values()}
    return out


def test_m10_reread_messages_are_not_counted(store: Store) -> None:
    from tests.test_b_resolver import NOW, built

    t = built(store)
    before = _numbers(store, NOW)
    for n in range(12):                                                 # the same messages relabelled "Yesterday"
        _reread(store, t["hemanth_dm"], n, me=bool(n % 2), at=NOW - DAY - n * 60)
        _reread(store, t["elina_dm"], 100 + n, me=bool(n % 2), at=NOW - 2 * DAY - n * 60)
    after = _numbers(store, NOW)
    assert after == before
    assert before["Hemanth"] and before["gather"][t["hemanth_dm"]][0] > 0


def test_m11_the_people_list_reads_call_digests_and_person_facts_once(store: Store, monkeypatch) -> None:
    from tests.test_b_resolver import built
    from yuki.memory import contacts as C
    from yuki.memory.api import MemoryClient

    built(store)
    counts = {"calls": 0, "facts": 0}
    real_seen, real_facts = C.call_people_seen, store.portrait_facts

    def seen(*a: Any, **k: Any) -> Any:
        counts["calls"] += 1
        return real_seen(*a, **k)

    def facts(*a: Any, **k: Any) -> Any:
        counts["facts"] += 1
        return real_facts(*a, **k)

    monkeypatch.setattr(C, "call_people_seen", seen)
    monkeypatch.setattr(store, "portrait_facts", facts)
    rows = MemoryClient(store).people_list(limit=60)
    assert len(rows) >= 3
    assert counts == {"calls": 1, "facts": 1}                          # once per list, not once per person


def test_m11_route_measures_and_the_watch_start_are_cached_until_a_message_arrives(store: Store, monkeypatch) -> None:
    from tests.test_b_resolver import NOW, _msg, built
    from yuki.memory import contacts as C

    t = built(store)
    person = next(p for p in C.people(store) if p.name == "Hemanth")
    first = C.rank_routes(store, person, now=NOW)
    old_mine = next(r.mine for r in first.routes if t["hemanth_dm"] in r.thread_ids)   # routes are filled in place
    C.watch_started(store)
    asked: list[str] = []
    real = store._query
    monkeypatch.setattr(store, "_query", lambda sql, *a, **k: (asked.append(sql), real(sql, *a, **k))[1])
    again = C.rank_routes(store, person, now=NOW)
    assert [r.mine for r in again.routes] == [r.mine for r in first.routes]
    assert not [q for q in asked if "SELECT is_me" in q]               # no message rows read again
    C.watch_started(store)
    assert not [q for q in asked if "min(first_seen)" in q]
    _msg(store, t["hemanth_dm"], 999, sender=None, me=True, at=NOW - 30, text="new one")
    asked.clear()
    newer = C.rank_routes(store, person, now=NOW)
    assert [q for q in asked if "SELECT is_me" in q]                   # a newer message: read again
    dm = next(r for r in newer.routes if t["hemanth_dm"] in r.thread_ids)
    assert dm.mine == old_mine + 1


def test_m11_the_home_refresh_waits_while_the_window_is_minimised(tmp_path: Path, monkeypatch) -> None:
    from yuki.ui import web_window

    # since the live refresh (42-window-live-refresh.md) there is no 60 s Home timer: Home is built on events, and
    # while the window can't be seen nothing is built; it catches up once when shown again
    import threading
    import time

    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               policy_path=tmp_path / "autonomy.json")
    assert win.window_shown()                                          # no window yet: never skipped by mistake
    monkeypatch.setattr(web_window, "DEBOUNCE_S", 0.02)
    monkeypatch.setattr(web_window, "MIN_GAP_S", {})
    refreshed: list[str] = []
    monkeypatch.setattr(win, "_enqueue", lambda name, *a, **k: refreshed.append(name))
    shown = {"now": False}
    monkeypatch.setattr(win, "window_shown", lambda: shown["now"])
    win.memory, win.booted, win.open_page = object(), True, "home"
    threading.Thread(target=win._scheduler, daemon=True).start()
    try:
        for _ in range(3):
            win.topic("cards")
        time.sleep(0.3)
        assert refreshed == []                                         # minimised: no refresh
        shown["now"] = True
        win.catch_up("restore")
        time.sleep(0.3)
        assert sorted(refreshed) == ["home", "shell"]                  # shown again: refreshed once
    finally:
        win._closing.set()
        with win._cv:
            win._cv.notify_all()


# ---------------------------------------------------------------------------
# M5: an empty people index is no index
# ---------------------------------------------------------------------------


class _Memory:
    def __init__(self, rows: list[dict] | None) -> None:
        self.now = time.time()
        if rows is not None:
            self.people_list = lambda limit=40: rows

    def portrait_facts_with_evidence(self, evidence_per_fact: int = 6) -> list[dict]:
        return [{"id": 22, "ref": "fact:22", "kind": "person", "subject": "Dev", "text": "Dev is the user's brother.",
                 "summary": "Your brother", "title": None, "aspect": None, "origin": "user", "confidence": 0.9,
                 "since": self.now - DAY, "evidence": []}]


def test_m5_an_empty_index_falls_back_to_the_portraits_people(tmp_path: Path, monkeypatch) -> None:
    from yuki.ui import web_data, web_window

    source, people = web_data.people_now(_Memory([]))
    assert source == "portrait" and people[0]["name"] == "Dev"
    source, people = web_data.people_now(_Memory([{"ref": "person:1", "name": "Riya", "routes": []}]))
    assert source == "index" and people[0]["name"] == "Riya"
    # the window: the tray's people op answers an empty index -> the portrait's people, not "Nobody yet"
    win = web_window.WebWindow(db_path=tmp_path / "mem" / "memory.db", ui_path=tmp_path / "ui.json", connect=False,
                               policy_path=tmp_path / "autonomy.json")
    win.client, win.ops, win.memory = SimpleNamespace(), {"people"}, _Memory([])
    monkeypatch.setattr(win, "request", lambda op, **kw: {"ok": True, "available": True, "people": []})
    source, people = win._people()
    assert source == "portrait" and people[0]["name"] == "Dev"
