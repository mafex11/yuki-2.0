"""capC2 (report 78 §5.1, §5.4, §8; report 98): links on exact machine joins only, transitions per app, migration 39.

Links: a paste joined to the user's latest copy by the clipboard sequence number; a file opened joined to the file
saved earlier by its path digest (a download is the browser's save); a hyperlink press joined to a browser's new page
in front within 3 s. Near misses make no link. Transitions: per app, element key -> next element key, with count,
median gap and last use, kept incrementally, never across a 2-minute pause.

Offline: temp databases, invented apps and names. The helpers here are shared by the other test_capC2 files.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from yuki.memory import trace_links as TL
from yuki.memory import trace_store
from yuki.memory.store import MIGRATIONS, Store
from yuki.memory.trace_privacy import name_class, normalise_name

NOW = 1_790_900_000.0   # 2026-10-02 (a fixed clock)
DAY = 86400.0


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def act(store: Any, at: float, process: str, kind: str, *, source: str = "user", resolved: str = "point",
        role: str | None = None, name: str | None = None, key: str | None = None, chord: str | None = None,
        position: str | None = None, clip_seq: int | None = None, file_digest: str | None = None,
        file_ext: str | None = None, facts: dict[str, Any] | None = None, page_key: str | None = None,
        typed: int | None = None, exit_code: int | None = None, outcome: str | None = "changed") -> int:
    """One trace row (and its element's registry use), as the trace worker writes them."""
    r = {"at": at, "process": process, "kind": kind, "source": source, "resolved": resolved, "role": role,
         "name_ciphertext": store._enc(name) if name and name_class(role) != "content" else None,
         "name_digest": store.cipher.digest("ui-name", process, role or "", normalise_name(name)) if name else None,
         "element_key": key, "chord": chord, "position": position, "outcome": outcome, "clip_seq": clip_seq,
         "file_digest": file_digest, "file_ext": file_ext, "facts": json.dumps(facts) if facts else None,
         "page_key": page_key, "typed": typed, "exit_code": exit_code}
    els = [{"element_key": key, "process": process, "role": role, "name_class": name_class(role), "name": name,
            "at": at}] if key else []
    return trace_store.write_batch(store, [r], els)[0]


def links(store: Any) -> list[tuple[int, int, str]]:
    return [(r[0], r[1], r[2]) for r in store._query("SELECT from_action, to_action, kind FROM ui_links ORDER BY id")]


# -- migration 39 --------------------------------------------------------------------------------------------------


def test_migration_39_on_a_fresh_database_and_again(tmp_path):
    s = Store.open(tmp_path / "fresh.db")
    try:
        assert len(MIGRATIONS) == 39 and s._query("PRAGMA user_version")[0][0] == 39
        tables = {r[0] for r in s._query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"ui_tasks", "ui_task_actions", "ui_families", "ui_family_members", "ui_transitions",
                "ui_task_runs"} <= tables
        assert "AUTOINCREMENT" in s._query("SELECT sql FROM sqlite_master WHERE name = 'ui_tasks'")[0][0]
        assert {"route", "vec_ciphertext", "apps"} <= {r[1] for r in s._query("PRAGMA table_info(ui_family_members)")}
        cols = {r[1] for r in s._query("PRAGMA table_info(ui_tasks)")}
        assert {"goal_ciphertext", "model_goal_ciphertext", "outcome_fact", "user_verdict", "vec_ciphertext"} <= cols
        assert "goal" not in cols and "label" not in {r[1] for r in s._query("PRAGMA table_info(ui_families)")}
        idx = {r[0] for r in s._query("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"ui_links_pair", "ui_actions_kind", "ui_actions_file", "ui_transitions_last"} <= idx
    finally:
        s.close()
    # the SQL is IF NOT EXISTS: running entry 39 again on the same file changes nothing
    conn = sqlite3.connect(tmp_path / "fresh.db")
    try:
        from yuki.memory.store import _split_sql

        for statement in _split_sql(MIGRATIONS[38]):
            conn.execute(statement)
    finally:
        conn.close()


def test_migration_39_from_38_keeps_the_trace_rows(tmp_path):
    path = tmp_path / "old.db"
    s = Store.open(path)
    act(s, NOW, "chatter", "click", role="Button", name="Send", key="k1")
    s.close()
    conn = sqlite3.connect(path)
    for t in ("ui_tasks", "ui_task_actions", "ui_families", "ui_family_members", "ui_transitions", "ui_task_runs"):
        conn.execute(f"DROP TABLE {t}")
    for i in ("ui_links_from", "ui_links_to", "ui_links_pair", "ui_actions_kind", "ui_actions_file"):
        conn.execute(f"DROP INDEX {i}")
    conn.execute("PRAGMA user_version=38")
    conn.commit()
    conn.close()
    s = Store.open(path)
    try:
        assert s._query("PRAGMA user_version")[0][0] == 39
        assert s._query("SELECT count(*) FROM ui_actions")[0][0] == 1
        assert s._query("SELECT count(*) FROM ui_tasks")[0][0] == 0
    finally:
        s.close()


# -- links ---------------------------------------------------------------------------------------------------------


def test_clipboard_links_join_only_the_latest_copy_with_the_same_number(store):
    c1 = act(store, NOW, "webby", "copy", resolved="event", clip_seq=41)
    p1 = act(store, NOW + 20, "chatter", "paste", resolved="focus", key="k-edit", role="Edit", clip_seq=41)
    p2 = act(store, NOW + 40, "notepadx", "paste", resolved="focus", clip_seq=41)        # the same copy again
    act(store, NOW + 60, "webby", "copy", resolved="event", clip_seq=42)
    act(store, NOW + 70, "chatter", "paste", resolved="focus", clip_seq=41)              # near miss: copied since
    act(store, NOW + 80, "chatter", "paste", resolved="focus", clip_seq=43)              # near miss: no such copy
    act(store, NOW + 90, "webby", "copy", resolved="event", clip_seq=44, source="yuki")
    act(store, NOW + 95, "chatter", "paste", resolved="focus", clip_seq=44)              # Yuki's copy: no link
    out = TL.update(store, now=NOW + 100)
    assert out["links"] == 2
    assert links(store) == [(c1, p1, "clipboard"), (c1, p2, "clipboard")]
    lag = store._query("SELECT lag_s FROM ui_links WHERE to_action = ?", (p1,))[0][0]
    assert lag == 20.0


def test_a_copy_and_paste_inside_one_app_is_no_link(store):
    act(store, NOW, "codepad", "copy", resolved="event", clip_seq=51)
    act(store, NOW + 4, "codepad", "paste", resolved="focus", key="k-ed", role="Edit", clip_seq=51)   # same app
    c = act(store, NOW + 10, "codepad", "copy", resolved="event", clip_seq=52)
    p = act(store, NOW + 14, "chatter", "paste", resolved="focus", key="k-edit", role="Edit", clip_seq=52)
    TL.update(store, now=NOW + 20)
    assert links(store) == [(c, p, "clipboard")]


def test_a_sequence_number_reused_after_a_restart_is_never_joined_to_an_old_copy(store):
    act(store, NOW, "webby", "copy", resolved="event", clip_seq=7)
    act(store, NOW + 3600, "webby", "copy", resolved="event", clip_seq=2)    # after a restart the numbers start again
    act(store, NOW + 3700, "chatter", "paste", resolved="focus", clip_seq=7)
    TL.update(store, now=NOW + 3800)
    assert links(store) == []


def test_file_links_save_then_open_and_a_download_then_open(store):
    s = act(store, NOW, "notepadx", "file_save", resolved="event", file_digest="d-note", file_ext=".txt",
            facts={"file_saved": "d-note"})
    o = act(store, NOW + 600, "explorer", "file_open", resolved="event", file_digest="d-note", file_ext=".txt",
            facts={"file_opened": "d-note"})
    dl = act(store, NOW + 700, "webby", "file_save", resolved="event", file_digest="d-pdf", file_ext=".pdf")
    o2 = act(store, NOW + 760, "readerx", "file_open", resolved="event", file_digest="d-pdf", file_ext=".pdf")
    act(store, NOW + 800, "readerx", "file_open", resolved="event", file_digest="d-other", file_ext=".pdf")  # never saved
    act(store, NOW + 810, "readerx", "file_open", resolved="event", file_digest="d-pdf", file_ext=".pdf",
        source="yuki")                                                                                   # Yuki's open
    TL.update(store, now=NOW + 900)
    assert links(store) == [(s, o, "file"), (dl, o2, "file")]


def test_url_links_a_hyperlink_then_a_browsers_new_page_within_3_seconds(store):
    h = act(store, NOW, "chatter", "click", role="Hyperlink", key="k-link", position="List")
    sw = act(store, NOW + 1.2, "webby", "switch", resolved="event", page_key="pg-a",
             facts={"window_verified": "webby", "page_reached": "pg-a"})
    act(store, NOW + 2.0, "webby", "nav", resolved="event", page_key="pg-a", facts={"page_reached": "pg-a"})
    # near misses: a page 3.1 s later, and a hyperlink in the browser itself (just navigation)
    act(store, NOW + 100, "chatter", "click", role="Hyperlink", key="k-link2")
    act(store, NOW + 103.1, "webby", "nav", resolved="event", page_key="pg-b", facts={"page_reached": "pg-b"})
    act(store, NOW + 200, "webby", "click", role="Hyperlink", key="k-in-page")
    act(store, NOW + 201, "webby", "nav", resolved="event", page_key="pg-c", facts={"page_reached": "pg-c"})
    # an Alt+Tab to a browser is a switch action, never a link
    act(store, NOW + 300, "chatter", "keys", resolved="focus", chord="Alt+Tab")
    act(store, NOW + 300.5, "webby", "switch", resolved="event", page_key="pg-c", facts={"window_verified": "webby"})
    TL.update(store, now=NOW + 400)
    assert links(store) == [(h, sw, "url")]                                   # one link per press


def test_a_switch_with_the_same_page_as_before_is_no_new_page(store):
    act(store, NOW - 50, "webby", "click", role="Button", key="k-b", page_key="pg-1")
    act(store, NOW, "chatter", "click", role="Hyperlink", key="k-link")
    act(store, NOW + 1, "webby", "switch", resolved="event", page_key="pg-1", facts={"window_verified": "webby"})
    TL.update(store, now=NOW + 10)
    assert links(store) == []
    act(store, NOW + 100, "chatter", "click", role="Hyperlink", key="k-link")
    sw = act(store, NOW + 101, "webby", "switch", resolved="event", page_key="pg-2", facts={"window_verified": "webby"})
    TL.update(store, now=NOW + 110)
    assert links(store)[-1][1:] == (sw, "url")


def test_links_are_found_once_and_incrementally(store):
    c = act(store, NOW, "webby", "copy", resolved="event", clip_seq=5)
    assert TL.update(store, now=NOW + 1)["links"] == 0
    p = act(store, NOW + 5, "chatter", "paste", resolved="focus", clip_seq=5)
    assert TL.update(store, now=NOW + 6)["links"] == 1
    assert TL.update(store, now=NOW + 7) == {"rows": 0}                        # nothing new: one indexed read
    assert links(store) == [(c, p, "clipboard")]


# -- transitions ---------------------------------------------------------------------------------------------------


def trans(store: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(r["process"], r["from_key"], r["to_key"]): dict(r) for r in store._query("SELECT * FROM ui_transitions")}


def test_transitions_per_app_with_count_median_gap_and_last_use(store):
    for i, base in enumerate((NOW, NOW + 1000, NOW + 2000)):
        act(store, base, "chatter", "click", role="Button", name="Attach", key="k-attach")
        act(store, base + 2 + i, "chatter", "click", role="Button", name="Send", key="k-send")
    TL.update(store, now=NOW + 3000)
    t = trans(store)
    assert t[("chatter", "k-attach", "k-send")]["n"] == 3
    assert t[("chatter", "k-attach", "k-send")]["median_gap_s"] == 3.0          # gaps 2, 3, 4
    assert t[("chatter", "k-attach", "k-send")]["last_at"] == NOW + 2004
    assert ("chatter", "k-send", "k-attach") not in t                           # 996 s apart: past the 2-minute cut
    assert all("Send" not in json.dumps(v) for v in t.values())                 # digests only


def test_transitions_follow_each_app_across_other_apps_and_skip_noise(store):
    act(store, NOW, "chatter", "click", role="Button", name="Attach", key="k-attach")
    act(store, NOW + 5, "webby", "click", role="Button", name="Back", key="k-back")
    act(store, NOW + 9, "chatter", "hover", role="Button", name="Mic", key="k-mic")             # a hover: no end
    act(store, NOW + 10, "chatter", "click", role="Button", name="Stale", key="k-stale", resolved="stale")
    act(store, NOW + 11, "chatter", "click", role="Button", name="Yukis", key="k-yuki", source="yuki")
    act(store, NOW + 12, "chatter", "click", role="Button", name="Send", key="k-send")
    TL.update(store, now=NOW + 20)
    assert set(trans(store)) == {("chatter", "k-attach", "k-send")}


def test_a_long_typing_run_is_input_all_along_for_the_transition_cut():
    row = lambda i, at, kind, key, end=None: {"id": i, "at": at, "ended_at": end, "process": "notepadx",  # noqa: E731
                                              "kind": kind, "source": "user", "resolved": "point", "element_key": key}
    rows = [row(1, 0.0, "click", "k-doc"), row(2, 1.0, "type", "k-body", end=151.0), row(3, 152.0, "click", "k-save")]
    pairs, last, last_input = TL.transition_pairs(rows)
    assert [(a, b) for _p, a, b, _g, _t in pairs] == [("k-doc", "k-body"), ("k-body", "k-save")]
    assert pairs[1][3] == 1.0 and last_input == 152.0                          # the gap from the run's end
    rows[1]["ended_at"] = None                                                 # a typing run of one moment
    pairs, _l, _i = TL.transition_pairs(rows)
    assert [(a, b) for _p, a, b, _g, _t in pairs] == [("k-doc", "k-body")]     # 151 s of no input: cut


def test_transitions_are_kept_incrementally_across_calls(store):
    act(store, NOW, "chatter", "click", role="Button", name="Attach", key="k-attach")
    TL.update(store, now=NOW + 1)
    act(store, NOW + 30, "chatter", "click", role="Button", name="Send", key="k-send")
    TL.update(store, now=NOW + 31)
    assert trans(store)[("chatter", "k-attach", "k-send")]["n"] == 1
    gaps = json.loads(trans(store)[("chatter", "k-attach", "k-send")]["gaps"])
    assert gaps == [30.0]
    for i in range(TL.GAPS_KEPT + 5):
        act(store, NOW + 1000 + i * 10, "chatter", "click", role="Button", name="Attach", key="k-attach")
        act(store, NOW + 1001 + i * 10, "chatter", "click", role="Button", name="Send", key="k-send")
    TL.update(store, now=NOW + 2000)
    row = trans(store)[("chatter", "k-attach", "k-send")]
    assert row["n"] == TL.GAPS_KEPT + 6 and len(json.loads(row["gaps"])) == TL.GAPS_KEPT
