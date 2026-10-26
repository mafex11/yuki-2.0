"""capC1 (report 96): migration 38 and the action trace's tables: writes, the 3-day name rule, retention, forgetting.

Temp databases only (a fresh Store per test); every name and app here is invented.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from yuki.memory import trace_store as ts
from yuki.memory.store import MIGRATIONS, Store
from yuki.memory.trace_privacy import COMMAND, CONTENT, TITLE

DAY = 86400.0


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _noon(days_ago: int) -> float:
    d = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return d.timestamp()


def _row(at: float, **kw) -> dict:
    base = {"at": at, "process": "fieldnotes", "kind": "click", "source": "user", "during_run": 0,
            "resolved": "point"}
    base.update(kw)
    return base


def test_migration_38_creates_the_trace_tables_on_a_fresh_database(store):
    assert store._query("PRAGMA user_version")[0][0] == len(MIGRATIONS) >= 38
    tables = {r[0] for r in store._query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ui_actions", "ui_elements", "ui_links", "ui_game_processes", "ui_files"} <= tables
    cols = {r[1] for r in store._query("PRAGMA table_info(ui_actions)")}
    # the design's columns plus the 7 Oct additions: hover, drag, travel and gap, what was on screen, the episode
    assert {"travel_px", "gap_ms", "dwell_ms", "end_element_key", "end_role", "capture_id", "page_key",
            "episode_id", "source", "during_run", "typed", "chord", "clip_seq", "facts"} <= cols
    assert not ({"text", "typed_text", "value"} & cols)        # no column could hold typed text


def test_migration_38_is_additive_and_reruns_safely(tmp_path):
    path = tmp_path / "old.db"
    Store.open(path).close()
    conn = sqlite3.connect(path)
    # 38 itself runs again on a file that has it (r30: rewinding user_version to len - 1 re-ran the last migration, not
    # 38, and mon1's migration 49, 22 Oct, ends with an ALTER that can't run twice). Every statement of 38 only creates
    # what is missing.
    conn.executescript(MIGRATIONS[37])
    conn.close()
    s = Store.open(path)
    try:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS)
        tables = {r[0] for r in s._query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"ui_actions", "ui_elements", "ui_links", "ui_game_processes", "ui_files"} <= tables
    finally:
        s.close()


def test_a_batch_writes_rows_and_counts_element_uses(store):
    ids = ts.write_batch(store, [_row(time.time(), element_key="k1", role="Button"),
                                 _row(time.time(), element_key="k1", role="Button")],
                         [{"element_key": "k1", "process": "fieldnotes", "role": "Button", "name_class": COMMAND,
                           "name": "Save draft", "aid": "saveBtn", "at": time.time()}] * 2)
    assert len(ids) == 2
    el = store._query("SELECT * FROM ui_elements WHERE element_key='k1'")[0]
    assert el["n"] == 2 and el["days"] == 1
    assert store._dec(el["name_ciphertext"]) == "Save draft"        # a command control: kept on first use
    assert store._dec(el["aid_ciphertext"]) == "saveBtn"


def test_a_title_name_is_promoted_only_after_three_distinct_days(store):
    use = {"element_key": "tab1", "process": "fieldnotes", "role": "TabItem", "name_class": TITLE,
           "name": "Quarterly plan"}
    for days_ago, expect_named in ((5, False), (5, False), (3, False), (1, True)):
        ts.write_batch(store, [], [{**use, "at": _noon(days_ago)}])
        el = store._query("SELECT days, name_ciphertext FROM ui_elements WHERE element_key='tab1'")[0]
        assert (el["name_ciphertext"] is not None) is expect_named, (days_ago, el["days"])
    assert store._query("SELECT days FROM ui_elements WHERE element_key='tab1'")[0][0] == 3


def test_a_content_control_never_keeps_a_name(store):
    for days_ago in (6, 4, 2, 0):
        ts.write_batch(store, [], [{"element_key": "item", "process": "fieldnotes", "role": "ListItem",
                                    "name_class": CONTENT, "name": "A friend's chat", "at": _noon(days_ago)}])
    el = store._query("SELECT days, name_ciphertext FROM ui_elements WHERE element_key='item'")[0]
    assert el["days"] == 4 and el["name_ciphertext"] is None


def test_the_purge_keeps_30_days_of_raw_rows_and_60_of_elements(store):
    now = time.time()
    ids = ts.write_batch(store, [_row(now - 31 * DAY), _row(now - 29 * DAY), _row(now)],
                         [{"element_key": "old", "process": "p", "role": "Button", "name_class": COMMAND,
                           "at": now - 61 * DAY},
                          {"element_key": "used", "process": "p", "role": "Button", "name_class": COMMAND,
                           "at": now - 59 * DAY}],
                         [{"row": 0, "kind": "flp", "outcome": "ok", "data": {"project": "x"}, "at": now - 31 * DAY}])
    store._write(lambda c: c.execute("INSERT INTO ui_links(from_action, to_action, kind, created_at) VALUES"
                                     " (?,?,?,?)", (ids[0], ids[2], "clipboard", now - 31 * DAY)))
    out = ts.purge(store, now=now)
    assert out == {"files": 1, "links": 1, "actions": 1, "elements": 1, "undo_images": 0}
    assert store._query("SELECT count(*) FROM ui_actions")[0][0] == 2
    assert [r[0] for r in store._query("SELECT element_key FROM ui_elements")] == ["used"]


def test_forget_an_hour_cascades_and_can_be_undone(store):
    now = time.time()
    hour = now - 3600
    rows = [_row(hour + 10, element_key="e1"), _row(hour + 20, element_key="e1"), _row(now - 7200, element_key="e1")]
    uses = [{"element_key": "e1", "process": "fieldnotes", "role": "Button", "name_class": COMMAND, "name": "Send",
             "at": r["at"]} for r in rows]
    ids = ts.write_batch(store, rows, uses, [{"row": 0, "kind": "flp", "outcome": "ok", "data": {"project": "x"}}])
    out = ts.forget(store, since=hour, until=hour + 3600)
    assert out["actions"] == 2 and out["files"] == 1
    assert store._query("SELECT count(*) FROM ui_actions")[0][0] == 1
    assert store._query("SELECT n FROM ui_elements WHERE element_key='e1'")[0][0] == 1   # recounted, still used
    diary = store._query("SELECT pass, action, text_ciphertext FROM memory_changelog WHERE id=?", (out["diary_id"],))[0]
    assert diary["pass"] == "trace" and diary["action"] == "forget"
    assert "Send" not in store._dec(diary["text_ciphertext"])                         # content-free words
    back = ts.undo_forget(store, out["diary_id"])
    assert back["ok"] and back["actions"] == 2
    assert sorted(r[0] for r in store._query("SELECT id FROM ui_actions")) == sorted(ids)
    assert ts.undo_forget(store, out["diary_id"])["ok"] is False                     # once only


def test_forget_an_app_removes_everything_learned_from_it(store):
    now = time.time()
    ts.write_batch(store, [_row(now, process="fieldnotes", element_key="a"), _row(now, process="sketchpad",
                                                                                element_key="b")],
                   [{"element_key": "a", "process": "fieldnotes", "role": "Button", "name_class": COMMAND, "at": now},
                    {"element_key": "b", "process": "sketchpad", "role": "Button", "name_class": COMMAND, "at": now}])
    out = ts.forget(store, process="fieldnotes")
    assert out["actions"] == 1 and out["elements"] == 1
    assert [r[0] for r in store._query("SELECT process FROM ui_actions")] == ["sketchpad"]
    assert [r[0] for r in store._query("SELECT element_key FROM ui_elements")] == ["b"]
    with pytest.raises(ValueError):
        ts.forget(store)


def test_episode_ids_are_attached_once_the_episode_is_final(store):
    now = time.time()
    ts.write_batch(store, [_row(now - 3000)])
    store._write(lambda c: c.execute(
        "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext, created_at)"
        " VALUES (?,?,?,?,?,?,?)", (now - 3600, now - 1800, now - 3600, now - 1800, 1, store._enc("x"), now)))
    assert ts.attach_episodes(store) == 1
    assert store._query("SELECT episode_id FROM ui_actions")[0][0] is not None


def _episode(store, start: float, end: float, *, expired: float | None = None) -> int:
    return int(store._write(lambda c: c.execute(
        "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext, created_at,"
        " expired_at) VALUES (?,?,?,?,?,?,?,?)",
        (start, end, start, end, 1, store._enc("x"), time.time(), expired)).lastrowid))


def test_rows_no_episode_covers_never_starve_newer_rows(store):
    """Review F5: 5,001 old rows outside any episode, then one newer row inside a final episode: it gets its id."""
    now = time.time()
    ts.write_batch(store, [_row(now - 20 * DAY + i) for i in range(5001)])
    (newer,) = ts.write_batch(store, [_row(now - 3000)])
    ts.attach_episodes(store, limit=1)                         # nothing final yet: the cursor stays
    ep = _episode(store, now - 3600, now - 1800)
    for _ in range(3):
        ts.attach_episodes(store)
    assert store._query("SELECT episode_id FROM ui_actions WHERE id=?", (newer,))[0][0] == ep
    assert store._query("SELECT count(*) FROM ui_actions WHERE episode_id IS NOT NULL")[0][0] == 1
    assert store.nudge_state(ts.EPISODE_CURSOR) == str(ep)
    # a later run of the same window replaces the episode: the row follows the new one
    store._write(lambda c: c.execute("UPDATE episodes SET expired_at=? WHERE id=?", (now, ep)))
    ep2 = _episode(store, now - 3600, now - 1800)
    assert ts.attach_episodes(store) == 1
    assert store._query("SELECT episode_id FROM ui_actions WHERE id=?", (newer,))[0][0] == ep2


def test_a_forgotten_trace_does_not_outlive_the_keep_in_undo_images(store):
    """Review F3: the forget's before-image is purged with the raw rows, undone or not."""
    now = time.time()
    ts.write_batch(store, [_row(now - 100, process="sketchpad", command_ciphertext=store._enc("invented cmd"))])
    ts.forget(store, process="sketchpad", now=now)
    assert store._query("SELECT count(*) FROM undo_images WHERE surface='trace'")[0][0] == 1
    later = now + 400 * DAY
    store.prune_undo_images(older_than_days=30, now=later)    # the shared pruner keeps the user's own images
    out = ts.purge(store, now=later)
    assert out["undo_images"] == 1
    assert store._query("SELECT count(*) FROM undo_images WHERE surface='trace'")[0][0] == 0
    assert ts.purge(store, now=now + 10 * DAY)["undo_images"] == 0


def test_what_was_on_screen_takes_two_queries_per_app_per_batch(store):
    """Review F9: capture ids per (app, batch), not per row; the right capture for each row."""
    now = time.time()
    tid = store._write(lambda c: c.execute(
        "INSERT INTO threads(thread_key, app, title_ciphertext, first_seen, last_seen, process) VALUES (?,?,?,?,?,?)",
        ("k-notes", "Field Notes", store._enc("Notes"), now, now, "fieldnotes")).lastrowid)
    cap = {}
    for k, at in (("old", now - 7 * 3600), ("a", now - 100), ("b", now - 50), ("c", now - 10)):
        cap[k] = store._write(lambda c, at=at: c.execute(
            "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash) VALUES (?,?,?,?,?,?)",
            (tid, at, "test", store._enc("x"), 1, f"h{at}")).lastrowid)
    queries = []
    real = store._query
    store._query = lambda *a, **k: queries.append(a[0]) or real(*a, **k)
    try:
        got = ts.capture_ids(store, [("fieldnotes", now - 60), ("fieldnotes", now - 40), ("fieldnotes", now - 5),
                                     ("otherapp", now - 5)])
    finally:
        store._query = real
    assert got[("fieldnotes", now - 60)] == cap["a"] and got[("fieldnotes", now - 40)] == cap["b"]
    assert got[("fieldnotes", now - 5)] == cap["c"] and got[("otherapp", now - 5)] is None
    assert len(queries) == 3                                   # 2 for the app with a span, 1 for a single row
    assert ts.capture_ids(store, [("fieldnotes", now - 6.5 * 3600)]) == {("fieldnotes", now - 6.5 * 3600): cap["old"]}
    assert ts.capture_ids(store, [("fieldnotes", now - 14 * 3600)]) == {("fieldnotes", now - 14 * 3600): None}


def test_yukis_untagged_steps_are_matched_to_its_run_but_a_takeover_never(store):
    now = time.time()
    ts.write_batch(store, [_row(now - 100, source="yuki", via="event_only", during_run=1, kind="invoke"),
                           _row(now - 99, source="user_injected", during_run=1, kind="keys"),
                           _row(now - 98, source="user", during_run=1)])
    store._write(lambda c: c.execute(
        "INSERT INTO runs(run_key, source, step, at, ended_at, tool, kind, app) VALUES (?,?,?,?,?,?,?,?)",
        ("agent:s:1", "agent", 2, now - 100.2, now - 98.5, "click_element", "action", "fieldnotes")))
    assert ts.match_runs(store, since=now - 600) == 2
    rows = store._query("SELECT source, run_key, run_step FROM ui_actions ORDER BY at")
    assert [(r[0], r[1], r[2]) for r in rows] == [("yuki", "agent:s:1", 2), ("yuki", "agent:s:1", 2),
                                                  ("user", None, None)]


def test_game_evidence_round_trips_through_its_table(store):
    ev = {"newgame": {"stretches": 2, "gpu_share": 61.0, "first_seen": 1.0, "last_seen": 2.0,
                      "evidence": "fullscreen_gpu"}}
    ts.save_game_evidence(store, ev)
    assert ts.load_game_evidence(store)["newgame"]["stretches"] == 2
    assert json.loads(store._query("SELECT evidence FROM ui_game_processes")[0][0])["gpu_share"] == 61.0


def test_summary_is_counts_only(store):
    now = time.time()
    ts.write_batch(store, [_row(now - 10, kind="type", typed=5, resolved="focus"), _row(now - 5)])
    s = ts.summary(store, since=now - 60)
    assert s["actions"] == 2 and s["typed"] == 5 and s["kind"] == {"type": 1, "click": 1}
    assert set(s["source"]) == {"user"}
