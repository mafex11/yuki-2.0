"""capC2 (report 78 §8, decision D4; report 98): retention and the forget cascade for links and tasks.

Tasks, their ranges, links and the pass's runs are kept 30 days; transitions are deleted after 60 days unused and
families after 90 (a family outlives its tasks: element keys and a short label). Forget-an-hour and forget-an-app
cascade to the tasks built on those rows, their families (rebuilt or deleted) and the transitions they made, and
Undo brings it all back.

Offline: temp databases, invented apps and goals.
"""

from __future__ import annotations

import json
from typing import Any

from test_c_practice import Words
from test_capC2_families import ATTACH, FILE, SEND, task
from test_capC2_links import DAY, NOW, act, store  # noqa: F401 - the store fixture
from yuki.memory import trace_families as F
from yuki.memory import trace_links as TL
from yuki.memory import trace_store as ts


def count(store: Any, table: str) -> int:
    return int(store._query(f"SELECT count(*) FROM {table}")[0][0])


def test_the_purge_keeps_tasks_30_days_transitions_60_and_families_90(store):
    now = NOW + 200 * DAY
    old = task(store, now - 31 * DAY, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    new = task(store, now - 29 * DAY, "You sent the weekly report to the team again", [ATTACH, FILE, SEND])
    F.refresh(store, Words(), now=now - 29 * DAY)
    store._write(lambda c: c.executemany("INSERT INTO ui_task_runs(at, cost_usd, ok) VALUES (?, 0.01, 1)",
                                         [(now - 31 * DAY,), (now - 1 * DAY,)]))
    store._write(lambda c: c.executemany(
        "INSERT INTO ui_transitions(process, from_key, to_key, n, last_at) VALUES ('chatter', ?, ?, 2, ?)",
        [("k-a", "k-b", now - 61 * DAY), ("k-b", "k-c", now - 59 * DAY)]))
    store._write(lambda c: c.executemany("INSERT INTO ui_families(n_tasks, last_at, updated_at) VALUES (1, ?, ?)",
                                         [(now - 91 * DAY, now - 91 * DAY), (now - 89 * DAY, now - 89 * DAY)]))
    out = ts.purge(store, now=now)
    assert out["tasks"] == 1 and out["task_actions"] >= 1 and out["task_runs"] == 1
    assert out["transitions"] == 1 and out["families"] == 1
    assert [r[0] for r in store._query("SELECT id FROM ui_tasks")] == [new]
    assert not store._query("SELECT 1 FROM ui_task_actions WHERE task_id = ?", (old,))
    assert [(r[0], r[1]) for r in store._query("SELECT from_key, to_key FROM ui_transitions")] == [("k-b", "k-c")]
    assert count(store, "ui_families") == 2                    # the report family (29 days) and the 89-day one
    assert ts.purge(store, now=now) == {"files": 0, "links": 0, "actions": 0, "elements": 0, "undo_images": 0}


def _two_days(store: Any) -> tuple[int, int]:
    a = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    b = task(store, NOW + DAY, "You sent the weekly report to the team chat", [ATTACH, FILE, SEND])
    act(store, NOW + 2 * DAY, "webby", "click", role="Button", name="Next", key="k-next")
    act(store, NOW + 2 * DAY + 3, "webby", "click", role="Button", name="Back", key="k-back")
    TL.update(store, now=NOW + 2 * DAY + 10)
    F.refresh(store, Words(), now=NOW + 2 * DAY + 20)
    return a, b


def test_forgetting_an_hour_takes_its_tasks_transitions_and_family_share_and_undo_brings_them_back(store):
    a, b = _two_days(store)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (a,))[0][0]
    assert store._query("SELECT n FROM ui_transitions WHERE from_key = 'k-attach' AND to_key = 'k-file'")[0][0] == 2
    before = {t: [dict(r) for r in store._query(f"SELECT * FROM {t} ORDER BY 1")]
              for t in ("ui_tasks", "ui_task_actions", "ui_families", "ui_transitions")}
    out = ts.forget(store, since=NOW - 10, until=NOW + 3600, now=NOW + 2 * DAY + 30)
    assert out["actions"] == 3 and out["tasks"] == 1 and out["families"] == 1
    assert [r[0] for r in store._query("SELECT id FROM ui_tasks")] == [b]
    assert store._query("SELECT n_tasks FROM ui_families WHERE id = ?", (fid,))[0][0] == 1
    assert store._query("SELECT n FROM ui_transitions WHERE from_key = 'k-attach' AND to_key = 'k-file'")[0][0] == 1
    assert store._query("SELECT n FROM ui_transitions WHERE from_key = 'k-next'")[0][0] == 1        # another hour
    back = ts.undo_forget(store, out["diary_id"], now=NOW + 2 * DAY + 40)
    assert back["ok"] and back["tasks"] == 1
    after = {t: [dict(r) for r in store._query(f"SELECT * FROM {t} ORDER BY 1")]
             for t in ("ui_tasks", "ui_task_actions", "ui_families", "ui_transitions")}
    assert after["ui_tasks"] == before["ui_tasks"] and after["ui_families"] == before["ui_families"]
    assert after["ui_transitions"] == before["ui_transitions"]
    key = lambda r: (r["task_id"], r["first_action"], r["detour"])                   # noqa: E731
    assert sorted(after["ui_task_actions"], key=key) == sorted(before["ui_task_actions"], key=key)


def test_forgetting_an_app_takes_every_task_family_and_transition_of_it(store):
    a, b = _two_days(store)
    mixed = task(store, NOW + 3 * DAY, "You copied a page into the chat", [
        ("webby", "click", "Button", "Copy link", "k-copy", None), ATTACH])
    F.refresh(store, Words(), now=NOW + 3 * DAY + 10)
    out = ts.forget(store, process="chatter", now=NOW + 3 * DAY + 20)
    assert out["tasks"] == 3                                                         # the mixed one too
    assert count(store, "ui_tasks") == 0
    assert all("chatter" not in json.loads(r["apps"] or "[]") for r in store._query("SELECT apps FROM ui_families"))
    assert not store._query("SELECT 1 FROM ui_transitions WHERE process = 'chatter'")
    assert store._query("SELECT count(*) FROM ui_transitions WHERE process = 'webby'")[0][0] == 1
    state = TL._load(store)
    assert "chatter" not in (state.get("last") or {})
    back = ts.undo_forget(store, out["diary_id"], now=NOW + 3 * DAY + 30)
    assert back["ok"] and count(store, "ui_tasks") == 3 and mixed
    assert store._query("SELECT count(*) FROM ui_transitions WHERE process = 'chatter'")[0][0] >= 1


# -- review fix F1: a family accumulates; its members outlive the 30-day task rows -------------------------------------


def test_a_family_keeps_its_count_and_usual_path_after_its_tasks_expire_and_grows_when_done_again(store):
    from test_c_practice import vec
    from test_capC2_families import three_reports
    from yuki.memory import practice as P

    reports = three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (reports[0],))[0][0]
    path_before = store._query("SELECT path_keys FROM ui_families WHERE id = ?", (fid,))[0][0]
    ts.purge(store, now=NOW + 35 * DAY)
    assert count(store, "ui_tasks") == 0 and count(store, "ui_family_members") == 3
    fam = store._query("SELECT n_tasks, path_keys, label_ciphertext FROM ui_families WHERE id = ?", (fid,))[0]
    assert fam["n_tasks"] == 3 and fam["path_keys"] == path_before and fam["label_ciphertext"]
    again = task(store, NOW + 36 * DAY, "You sent the weekly report to the team", [ATTACH, FILE, SEND],
                 outcome="field_submitted")
    F.refresh(store, Words(), now=NOW + 36 * DAY + 100)
    assert store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (again,))[0][0] == fid
    fam = store._query("SELECT n_tasks, path_keys, label_task FROM ui_families WHERE id = ?", (fid,))[0]
    assert fam["n_tasks"] == 4 and fam["label_task"] == again
    assert [nd["k"] for nd in F.usual(json.loads(fam["path_keys"]), 4)] == ["k-attach", "k-file",
                                                                             "chatter|keys|Enter"]
    out = P.relevant(store, query_vec=vec("send the weekly report to the team"), apps_in_play=["chatter"],
                     now=NOW + 36 * DAY + 200)
    assert out["path"] is not None and out["path"]["tasks"] == 4


def test_a_member_older_than_90_days_leaves_and_the_family_is_rebuilt_from_the_rest(store):
    old = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    F.refresh(store, Words(), now=NOW + 10)
    new = task(store, NOW + 80 * DAY, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    F.refresh(store, Words(), now=NOW + 80 * DAY + 10)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (new,))[0][0]
    assert store._query("SELECT n_tasks FROM ui_families WHERE id = ?", (fid,))[0][0] == 2
    out = ts.purge(store, now=NOW + 91 * DAY)
    assert out["family_members"] == 1
    assert [r[0] for r in store._query("SELECT task_id FROM ui_family_members")] == [new] and old
    assert store._query("SELECT n_tasks FROM ui_families WHERE id = ?", (fid,))[0][0] == 1
    ts.purge(store, now=NOW + 80 * DAY + 91 * DAY)
    assert count(store, "ui_families") == 0 and count(store, "ui_family_members") == 0


def test_forgetting_an_hour_or_an_app_takes_the_members_of_expired_tasks_too_and_undo_brings_them_back(store):
    a = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    b = task(store, NOW + DAY, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    F.refresh(store, Words(), now=NOW + DAY + 10)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (a,))[0][0]
    later = NOW + 30 * DAY
    ts.purge(store, now=later + 0.5 * DAY)                      # task a expired (its member is kept), b is not
    assert [r[0] for r in store._query("SELECT id FROM ui_tasks")] == [b] and count(store, "ui_family_members") == 2
    # the hour of task a: its rows are gone with the purge, so the forget takes the member by its span
    store._write(lambda c: c.execute("INSERT INTO ui_actions(at, process, kind, source) VALUES (?, 'chatter',"
                                     " 'click', 'pointer')", (NOW + 1,)))
    out = ts.forget(store, since=NOW - 10, until=NOW + 3600, now=later + DAY)
    assert out["family_members"] == 1 and out["tasks"] == 0
    assert [r[0] for r in store._query("SELECT task_id FROM ui_family_members")] == [b]
    assert store._query("SELECT n_tasks FROM ui_families WHERE id = ?", (fid,))[0][0] == 1
    back = ts.undo_forget(store, out["diary_id"], now=later + DAY + 10)
    assert back["ok"] and back["family_members"] == 1
    out = ts.forget(store, process="chatter", now=later + DAY + 20)
    assert out["family_members"] == 2 and count(store, "ui_family_members") == 0 and count(store, "ui_families") == 0
