"""Recently done in second person (leftMem): done commitments get a short title too.

The titles pass titles a commitment while it is open and, once done, while it is on the Work page's Recently done
(``COMMIT_DONE_TITLE_DAYS``), told it is done; the prompt carries worked examples of memory's third-person text said
to the user. The one-off backfill (``yuki.memory.done_titles``) titles every older done commitment stored without one:
dry run -> saved plan -> apply exactly that -> undo.

Offline: temp databases and scripted model answers (invented names), no network, no real memory.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from yuki.memory import titles
from yuki.memory.store import COMMIT_DONE_TITLE_DAYS, Store

DAY = 86400.0


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """The bits of PortraitWorker the titles passes use, with scripted model answers."""

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

    def _seal(self, value):
        return "sealed"


def _commitments(store: Store) -> dict[str, int]:
    now = time.time()
    ids = {
        "open": store.add_conversation_fact("commitment", "The user will book the invented dentist", owner="user",
                                            at=now - 2 * DAY, origin="model"),
        "done_recent": store.add_conversation_fact("commitment", "The user will send their landlord the rent receipt",
                                                   owner="user", at=now - 3 * DAY, origin="model"),
        "done_yuki": store.add_conversation_fact("commitment", "Yuki will remind the user to call their sister",
                                                 owner="yuki", at=now - 3 * DAY, origin="model"),
        "done_old": store.add_conversation_fact("commitment", "The user will renew their invented gym pass",
                                                owner="user", at=now - 40 * DAY, origin="model"),
        "dropped": store.add_conversation_fact("commitment", "The user will water the invented plants",
                                               owner="user", at=now - 2 * DAY, origin="model"),
    }
    store.end_conversation_fact(ids["done_recent"], "done", at=now - 1 * DAY, note="done, by Yuki's to-do check")
    store.end_conversation_fact(ids["done_yuki"], "done", at=now - 2 * DAY, note="done")
    store.end_conversation_fact(ids["done_old"], "done", at=now - 30 * DAY, note="done")
    store.end_conversation_fact(ids["dropped"], "dropped", at=now - 1 * DAY, note="no longer needed")
    return ids


def test_recently_done_window_is_the_work_pages():
    from yuki.memory.nudges import DONE_DAYS

    assert COMMIT_DONE_TITLE_DAYS == DONE_DAYS


def test_done_commitments_on_recently_done_are_pending_told_they_are_done(store):
    ids = _commitments(store)
    missing = set(store.labels_missing(["commit"])["commit"])
    assert missing == {ids["open"], ids["done_recent"], ids["done_yuki"]}  # not the old one, not a dropped one
    pending = {p.item_id: p for p in titles.Titler(_Worker(store)).pending() if p.surface == "commit"}
    assert set(pending) == missing
    assert pending[ids["done_recent"]].what == "the user's own to-do, already done"
    assert pending[ids["done_yuki"]].what == "Yuki's own promise, already done"
    assert pending[ids["open"]].what == "the user's own to-do"
    redo = {p.item_id for p in titles.Titler(_Worker(store)).pending(redo=True) if p.surface == "commit"}
    assert ids["done_old"] not in redo and ids["dropped"] not in redo     # the backfill's, never the live pass's


def test_the_titles_pass_writes_done_titles_and_the_app_shows_them(store):
    from yuki.memory.api import MemoryClient

    ids = _commitments(store)
    answer = {"labels": [{"id": f"C{ids['open']}", "label": "Book the dentist"},
                         {"id": f"C{ids['done_recent']}", "label": "Send your landlord the rent receipt"},
                         {"id": f"C{ids['done_yuki']}", "label": "Remind you to call your sister"}]}
    worker = _Worker(store, [answer])
    result = titles.Titler(worker).run(what="labels")
    assert result.ok and result.written == 3 and store.labels_missing(["commit"])["commit"] == []
    user = worker.calls[0]["user"]
    assert f"C{ids['done_recent']} [the user's own to-do, already done]" in user
    assert store.conversation_fact(ids["done_recent"]).title == "Send your landlord the rent receipt"
    done = {t["id"]: t for t in MemoryClient(store).todos(include_done=True) if t["status"] == "done"}
    assert done[f"commit:{ids['done_recent']}"]["title"] == "Send your landlord the rent receipt"


def test_a_newly_closed_commitment_without_a_title_is_picked_up(store):
    c = store.add_conversation_fact("commitment", "The user will post the invented parcel", owner="user",
                                    at=time.time() - 3600, origin="model")
    store.end_conversation_fact(c, "done", note="done, by Yuki's to-do check")
    assert store.labels_missing(["commit"])["commit"] == [c]


def test_the_prompt_teaches_second_person_titles_with_worked_examples():
    prompt = titles.SYSTEM_PROMPT
    assert "\"The user will send their landlord the rent receipt by Friday\" -> \"Send your landlord the rent " \
           "receipt\"" in prompt
    assert "\"Yuki will remind the user to call their sister at 6 PM\" -> \"Remind you to call your sister\"" in prompt
    assert "already done keeps the title of what was to be done" in prompt


# -- the backfill ---------------------------------------------------------------------------------------------------------


def test_backfill_dry_run_plan_apply_undo(store, tmp_path):
    from yuki.memory.done_titles import DONE_TITLES_PASS, DoneTitles, untitled_done
    from yuki.memory.passplan import load_plan, save_plan

    ids = _commitments(store)
    titled = store.add_conversation_fact("commitment", "The user will pay the invented bill", owner="user",
                                         at=time.time() - 50 * DAY, origin="model", title="Pay the bill")
    store.end_conversation_fact(titled, "done", at=time.time() - 45 * DAY, note="done")
    targets = [ids["done_recent"], ids["done_yuki"], ids["done_old"]]
    assert sorted(f.id for f in untitled_done(store)) == sorted(targets)  # done, untitled, any age; never others
    answer = {"labels": [{"id": f"C{ids['done_recent']}", "label": "Send your landlord the rent receipt"},
                         {"id": f"C{ids['done_yuki']}", "label": "Remind you to call your sister"},
                         {"id": f"C{ids['done_old']}", "label": "Renew your gym pass"}]}
    worker = _Worker(store, [answer])
    dry = DoneTitles(worker).run(dry_run=True)
    assert dry.ok and dry.untitled == 3 and dry.written == 0 and len(dry.decisions) == 3 and dry.run_id is None
    assert all(store.conversation_fact(i).title is None for i in targets)  # a dry run writes nothing
    assert worker.calls[0]["system"] == titles.SYSTEM_PROMPT and worker.calls[0]["purpose"] == "titles"
    assert "already done" in worker.calls[0]["user"]
    path = save_plan(store, DONE_TITLES_PASS, dry.decisions, tmp_path / "plan.json")
    assert "rent receipt" not in path.read_text(encoding="utf-8")         # the plan is encrypted with memory's key
    # meanwhile the titles pass titled one: the plan never overwrites it
    store.set_labels("commit", {ids["done_recent"]: {"title": "Send the rent receipt"}})
    applied = DoneTitles(_Worker(store)).run(plan=load_plan(store, DONE_TITLES_PASS, path))
    assert applied.ok and applied.written == 2 and applied.calls == 0        # no model call on apply
    assert store.conversation_fact(ids["done_recent"]).title == "Send the rent receipt"
    assert store.conversation_fact(ids["done_old"]).title == "Renew your gym pass"
    assert store.conversation_fact(ids["done_yuki"]).title == "Remind you to call your sister"
    assert store.conversation_fact(ids["open"]).title is None              # an open one is the live pass's
    undone = DoneTitles(_Worker(store)).undo(applied.run_id)
    assert undone == {"cleared": 2, "diverged": 0}
    assert store.conversation_fact(ids["done_old"]).title is None
    assert store.conversation_fact(ids["done_recent"]).title == "Send the rent receipt"


def test_backfill_retries_a_title_over_its_limit_and_skips_bad_ones(store):
    from yuki.memory.done_titles import DoneTitles

    ids = _commitments(store)
    long = "Send your landlord the rent receipt for the flat today"
    first = {"labels": [{"id": f"C{ids['done_recent']}", "label": long},
                        {"id": f"C{ids['done_yuki']}", "label": "Remind you to call your sister"},
                        {"id": f"C{ids['done_old']}", "label": ""}]}
    again = {"labels": [{"id": f"C{ids['done_recent']}", "label": "Send your landlord the receipt"}]}
    r = DoneTitles(_Worker(store, [first, again])).run()
    assert r.ok and r.retried == 1 and r.written == 2 and r.calls == 2
    assert store.conversation_fact(ids["done_recent"]).title == "Send your landlord the receipt"
    assert store.conversation_fact(ids["done_old"]).title is None and r.rejected


def test_backfill_plan_skips_a_commitment_whose_text_changed(store):
    from yuki.memory.done_titles import DoneTitles

    ids = _commitments(store)
    plan = [{"id": ids["done_old"], "text": "something else", "title": "Renew your gym pass"},
            {"id": ids["open"], "text": "The user will book the invented dentist", "title": "Book the dentist"}]
    r = DoneTitles(_Worker(store)).run(plan=plan)
    assert r.ok and r.written == 0                                         # changed text; an open one: never


def test_backfill_command_needs_a_mode(capsys):
    from yuki.memory.done_titles import main

    with pytest.raises(SystemExit):
        main([])
    assert "say --dry-run, --apply FILE or --undo RUN_ID" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--dry-run", "--apply", "x.json"])
    assert "leave out --dry-run" in capsys.readouterr().err


def test_backfill_command_applies_a_saved_plan(tmp_path):
    from yuki.memory.done_titles import DONE_TITLES_PASS, main
    from yuki.memory.passplan import save_plan

    db = tmp_path / "copy.db"
    s = Store.open(db)
    try:
        ids = _commitments(s)
        path = save_plan(s, DONE_TITLES_PASS, [{"id": ids["done_old"], "text": s.conversation_fact(ids["done_old"]).text,
                                                "title": "Renew your gym pass"}], tmp_path / "plan.json")
    finally:
        s.close()
    assert main(["--db", str(db), "--apply", str(path)]) == 0
    s = Store.open(db)
    try:
        assert s.conversation_fact(ids["done_old"]).title == "Renew your gym pass"
    finally:
        s.close()
