"""Recently done in second person (leftMem): done commitments get a short title too.

The titles pass titles a commitment while it is open and, once done, while it is on the Work page's Recently done
(``COMMIT_DONE_TITLE_DAYS``), told it is done; the prompt carries worked examples of memory's third-person text said
to the user. (The one-off backfill, ``yuki.memory.done_titles``, was deleted on 18 Oct with its tests.)

Offline: temp databases and scripted model answers (invented names), no network, no real memory.
"""

from __future__ import annotations

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
