"""A reopened promise stays open (leftMem, migration 37).

On the Work page the user can press Not done yet on a promise Yuki saw kept (or Put it back, or Undo). Memory then
closes it again only on NEW evidence dated after that moment: never the mirror that was done before, never the journal
entry or exchange that closed it the first time, never a call digest's (older) joke mark.

Temp databases and stub models only; every name and message is invented; nothing touches the desktop or a model.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import MIGRATIONS, Store

T0 = datetime(2026, 10, 1, 21, 0).timestamp()
BACK = T0 + 6 * 3600.0          # when the user pressed Not done yet (set exactly after the real press)


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class _Worker:
    """What the commitment check needs of the conversation worker: its store, model call, log and seal."""

    def __init__(self, store: Store, answer: dict[str, Any]) -> None:
        self.store = store
        self.answer = answer
        self.calls: list[str] = []
        self.logged: list[tuple[str, dict]] = []

    def _call(self, purpose, system, user, tool, call, model=None):
        self.calls.append(user)
        call.cost_usd = 0.001
        return self.answer, SimpleNamespace(content=[]), {}

    def _log(self, type, **fields):
        self.logged.append((type, fields))

    def _seal(self, value):
        return "sealed"


def _not_done_yet(store: Store, cid: int, at: float = BACK) -> None:
    """The Work page's Not done yet (memory's reopen_item at any age), then its moment set to ``at``."""
    before = time.time()
    assert MemoryClient(store).reopen_item(f"commit:{cid}", any_age=True) == f"commit:{cid}"
    f = store.conversation_fact(cid)
    assert f.status == "active" and f.valid_to is None and f.end_note is None
    assert f.reopened_at is not None and before - 1 <= f.reopened_at <= time.time() + 1   # the press set it
    store._write(lambda conn: conn.execute("UPDATE conversation_facts SET reopened_at=? WHERE id=?", (at, cid)))


def _mirrors(store: Store) -> tuple[int, int]:
    tid = store.add_turn("s", 1, T0, "tag Neha in the invented group and ask about Friday", "On it.", [], "final")
    store.flush_turns()
    yuki = store.add_conversation_fact("commitment", "Send the message tagging Neha", owner="yuki",
                                       source_turn_ids=[tid], at=T0, origin="model")
    user = store.add_conversation_fact("commitment", "The user will have Yuki tag Neha", owner="user",
                                       source_turn_ids=[tid], at=T0, origin="model")
    return yuki, user


# -- mirrors ----------------------------------------------------------------------------------------------------------


def test_a_reopened_mirror_is_not_closed_again_by_the_same_done_mirror(store):
    from yuki.memory.commitments import close_mirrors

    yuki, user = _mirrors(store)
    store.end_conversation_fact(yuki, "done", at=T0 + 60, note="The message was sent")
    assert close_mirrors(store, now=T0 + 120) == [{"item": user, "mirror": yuki}]
    _not_done_yet(store, user)
    assert close_mirrors(store, now=BACK + 600) == []                       # the same done mirror: stays open
    assert store.conversation_fact(user).status == "active"


def test_a_reopened_mirror_closes_on_its_mirror_done_after_the_reopen(store):
    from yuki.memory.commitments import close_mirrors

    yuki, user = _mirrors(store)
    store.end_conversation_fact(yuki, "done", at=T0 + 60, note="The message was sent")
    close_mirrors(store, now=T0 + 120)
    _not_done_yet(store, user)
    _not_done_yet(store, yuki)                                              # both put back
    store.end_conversation_fact(yuki, "done", at=BACK + 900, note="The message was sent again, for real")
    assert close_mirrors(store, now=BACK + 1000) == [{"item": user, "mirror": yuki}]
    assert store.conversation_fact(user).status == "done"


def test_a_mirror_done_late_on_old_evidence_does_not_count(store):
    """The mirror's own close is dated by its evidence: written after the reopen but resting on older evidence, it is
    not new evidence."""
    from yuki.memory.commitments import close_mirrors

    yuki, user = _mirrors(store)
    store.end_conversation_fact(user, "done", at=T0 + 60, note="done, by Yuki's to-do check (J3): sent")
    _not_done_yet(store, user)
    store.end_conversation_fact(yuki, "done", at=T0 + 60, note="done, by Yuki's to-do check (J3): sent")
    assert close_mirrors(store, now=BACK + 2000) == []
    assert store.conversation_fact(user).status == "active"


# -- the check ----------------------------------------------------------------------------------------------------------


def test_the_check_never_closes_a_reopened_one_on_the_evidence_that_closed_it(store):
    from yuki.memory.commitments import CommitmentChecker

    c = store.add_conversation_fact("commitment", "The user will send Mira the slides", owner="user", at=T0,
                                    origin="model")
    old = store.add_journal(T0 + 3600, None, "Gmail", "The user sent Mira an email.", 6)
    answer = {"decisions": [{"commitment": c, "status": "done", "journal_ids": [old], "turn_ids": [],
                             "done_ids": [], "reason": "sent it"}]}
    assert CommitmentChecker(_Worker(store, answer)).check(T0 + 7200, force=True).closed == [
        {"item": c, "status": "done"}]
    _not_done_yet(store, c)
    store.add_journal(BACK + 600, None, "Spotify", "The user played music.", 3)   # newer, unrelated
    worker = _Worker(store, answer)                                          # the model cites the old entry again
    result = CommitmentChecker(worker).check(BACK + 3600, force=True)
    assert result.closed == [] and any(f"C{c}" in r and "after the user put it back" in r for r in result.rejected)
    assert store.conversation_fact(c).status == "active"
    message = worker.calls[0]
    assert f"C{c} (owner user" in message and "put back by the user" in message
    assert f"J{old} " not in message                                        # older than the put-back: not shown


def test_the_check_closes_a_reopened_one_on_new_evidence(store):
    from yuki.memory.commitments import CommitmentChecker

    c = store.add_conversation_fact("commitment", "The user will send Mira the slides", owner="user", at=T0,
                                    origin="model")
    store.end_conversation_fact(c, "done", at=T0 + 3600, note="done, by Yuki's to-do check (J1): sent")
    _not_done_yet(store, c)
    new = store.add_journal(BACK + 1800, None, "Gmail", "The user sent Mira the slides deck.", 6)
    answer = {"decisions": [{"commitment": c, "status": "done", "journal_ids": [new], "turn_ids": [],
                             "done_ids": [], "reason": "sent the deck"}]}
    result = CommitmentChecker(_Worker(store, answer)).check(BACK + 3600, force=True)
    assert result.closed == [{"item": c, "status": "done"}]
    f = store.conversation_fact(c)
    assert f.status == "done" and f.valid_to == pytest.approx(BACK + 1800) and f"J{new}" in f.end_note


def test_a_duplicate_done_before_the_reopen_does_not_close_it(store):
    from yuki.memory.commitments import CommitmentChecker

    c = store.add_conversation_fact("commitment", "Send the invented report", owner="user", at=T0, origin="call")
    dup = store.add_conversation_fact("commitment", "Send the invented report to Omar", owner="user", at=T0 + 10,
                                      origin="call")
    store.end_conversation_fact(dup, "done", at=T0 + 3600, note="sent")
    store.end_conversation_fact(c, "done", at=T0 + 3600, note="done, as a duplicate (commit:2)")
    _not_done_yet(store, c)
    store.add_journal(BACK + 60, None, "Gmail", "a newer entry", 5)
    answer = {"decisions": [{"commitment": c, "status": "done", "journal_ids": [], "turn_ids": [],
                             "done_ids": [dup], "reason": "the same task, done"}]}
    result = CommitmentChecker(_Worker(store, answer)).check(BACK + 600, force=True)
    assert result.closed == [] and store.conversation_fact(c).status == "active"


def test_an_exchange_from_before_the_reopen_does_not_close_it(store):
    from yuki.memory.commitments import CommitmentChecker

    c = store.add_conversation_fact("commitment", "Yuki will message Arjun about the trek", owner="yuki", at=T0,
                                    origin="model")
    old_t = store.add_turn("s", 2, T0 + 300, "message Arjun", "Sent it.", ["type_text", "press enter"], "final")
    store.flush_turns()
    store.end_conversation_fact(c, "done", at=T0 + 300, note="done, by Yuki's to-do check (T1): sent")
    _not_done_yet(store, c)
    new_t = store.add_turn("s", 3, BACK + 300, "now really message Arjun", "Sent it.", ["type_text"], "final")
    store.flush_turns()
    answer = {"decisions": [{"commitment": c, "status": "done", "journal_ids": [], "turn_ids": [old_t],
                             "done_ids": [], "reason": "sent"}]}
    result = CommitmentChecker(_Worker(store, answer)).check(BACK + 600, force=True)
    assert result.closed == []
    answer["decisions"][0]["turn_ids"] = [new_t]
    result = CommitmentChecker(_Worker(store, answer)).check(BACK + 700, force=True)
    assert result.closed == [{"item": c, "status": "done"}]


def test_commitments_never_put_back_close_as_before(store):
    """No reopen marker: evidence after the commitment itself counts, exactly as before."""
    from yuki.memory.commitments import CommitmentChecker, open_since

    c = store.add_conversation_fact("commitment", "Pay the invented rent", owner="user", at=T0, origin="model")
    j = store.add_journal(T0 + 60, None, "Bank", "The user paid the rent.", 6)
    f = store.conversation_fact(c)
    assert f.reopened_at is None and open_since(f) == f.valid_from
    answer = {"decisions": [{"commitment": c, "status": "done", "journal_ids": [j], "turn_ids": [],
                             "done_ids": [], "reason": "paid"}]}
    worker = _Worker(store, answer)
    assert CommitmentChecker(worker).check(T0 + 600, force=True).closed == [{"item": c, "status": "done"}]
    assert "put back by the user" not in worker.calls[0]


# -- the joke mark, an update, the migration ----------------------------------------------------------------------------


def test_a_reopened_call_commitment_is_not_ended_as_a_joke_again(store):
    from yuki.memory.commitments import close_joke_commitments

    said = T0 + 300.0
    cid = store.start_call(app="Discord", process="discord", via="screen", at=T0)
    store.update_call(cid, ended_at=T0 + 3600)
    store.save_call_digest(cid, app="Discord", capture_ids=[], status="ok", gist="Banter with Kabir.",
                           blocks=[{"call_id": cid, "start": said, "end": said + 5, "topic": "banter",
                                    "lines": [{"at": said, "text": "I'll send the invented form tonight",
                                               "nature": "humor"}]}],
                           started_at=T0, ended_at=T0 + 3600, speech_s=60.0, utterances=1, kept=1, model="m")
    c = store.add_conversation_fact("commitment", "Send Kabir the invented form tonight", origin="call",
                                    owner="user", at=said, quote="I'll send the invented form tonight")
    assert close_joke_commitments(store, now=T0 + 7200) == [{"item": c, "call": cid}]
    _not_done_yet(store, c)                                                 # "it wasn't a joke": put back
    assert close_joke_commitments(store, now=BACK + 7200) == []
    assert store.conversation_fact(c).status == "active"


def test_a_new_version_keeps_the_reopen_marker(store):
    c = store.add_conversation_fact("commitment", "Send Mira the slides", owner="user", at=T0, origin="model")
    store.end_conversation_fact(c, "done", at=T0 + 60, note="done, by Yuki's to-do check (J1)")
    _not_done_yet(store, c)
    new = store.add_conversation_fact("commitment", "Send Mira the slides by Monday", owner="user", at=BACK + 60,
                                      origin="model", supersedes=c)
    assert new != c and store.conversation_fact(c).status == "superseded"
    assert store.conversation_fact(new).reopened_at == pytest.approx(BACK)
    plain = store.add_conversation_fact("commitment", "Water the invented plants", owner="user", at=T0)
    assert store.conversation_fact(plain).reopened_at is None


def test_the_undo_of_the_users_own_end_marks_it_too(store):
    m = MemoryClient(store)
    c = store.add_conversation_fact("commitment", "Book the invented dentist", owner="user", origin="model")
    assert m.end_todo(f"commit:{c}", "done") == f"commit:{c}"
    assert m.reopen_item(f"commit:{c}") == f"commit:{c}"                   # Undo within 10 minutes
    assert store.conversation_fact(c).reopened_at is not None


def test_migration_adds_the_column_to_an_existing_database(tmp_path: Path):
    path = tmp_path / "old.db"
    s = Store.open(path)
    s.close()
    conn = sqlite3.connect(path)   # back to the shape before migration 37 (v36): the column is new in 37
    conn.execute("ALTER TABLE conversation_facts DROP COLUMN reopened_at")
    conn.execute(f"PRAGMA user_version={len(MIGRATIONS) - 1}")
    conn.commit()
    conn.close()
    s = Store.open(path)
    try:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS)
        cols = {r[1] for r in s._query("PRAGMA table_info(conversation_facts)")}
        assert "reopened_at" in cols
    finally:
        s.close()


# -- the conversation extractor (second review R1) ---------------------------------------------------------------------


def _extract_ops(store: Store, ops: list[dict[str, Any]], turn_ids: list[int]):
    from yuki.memory.conversations import ConversationWorker

    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids(turn_ids)
    return worker.validate(ops, turns, store.conversation_facts(), worker.cards_for(turns))


def _op(op: str, item: int, n: int, end_as: str = "", text: str = "") -> dict[str, Any]:
    return {"op": op, "item": item, "kind": "commitment" if op == "UPDATE" else "", "subject": "", "text": text,
            "quote": "", "due": "", "end_as": end_as, "card": 0, "exchanges": [n], "reason": "r", "owner": "user"}


def test_the_extractor_never_closes_a_reopened_one_on_an_exchange_from_before(store):
    """A done-sounding exchange still waiting for extraction when the user pressed Not done yet (a failed batch is
    retried later) no longer closes, revokes or rewrites the promise; one dated after the put-back still does."""
    c = store.add_conversation_fact("commitment", "The user will send Mira the slides", owner="user", at=T0,
                                    origin="model")
    old_t = store.add_turn("s", 4, T0 + 3600, "sent Mira the slides", "Nice.", [], "final")
    store.flush_turns()
    store.end_conversation_fact(c, "done", at=T0 + 3600, note="done, by Yuki's to-do check (T1): sent")
    _not_done_yet(store, c)
    new_t = store.add_turn("s", 5, BACK + 300, "ok now I really sent Mira the slides", "Nice.", [], "final")
    store.flush_turns()
    for op in (_op("INVALIDATE", c, 1, "done"), _op("INVALIDATE", c, 1, "revoked"),
               _op("UPDATE", c, 1, text="The user sent Mira the slides")):
        changes, records = _extract_ops(store, [op], [old_t, new_t])
        assert changes == [] and records[0].status == "rejected: the cited exchanges are older than when the user put it back"
    changes, records = _extract_ops(store, [_op("INVALIDATE", c, 2, "done")], [old_t, new_t])
    assert [(ch.op, ch.end_as) for ch in changes] == [("INVALIDATE", "done")] and changes[0].at == BACK + 300


def test_the_extractor_closes_one_never_put_back_as_before(store):
    c = store.add_conversation_fact("commitment", "The user will send Mira the slides", owner="user", at=T0,
                                    origin="model")
    t = store.add_turn("s", 4, T0 + 3600, "sent Mira the slides", "Nice.", [], "final")
    store.flush_turns()
    changes, _records = _extract_ops(store, [_op("INVALIDATE", c, 1, "done")], [t])
    assert [(ch.op, ch.end_as) for ch in changes] == [("INVALIDATE", "done")]


def test_the_extractor_prompt_shows_the_put_back(store):
    from yuki.memory.conversations import EXTRACT_SYSTEM_PROMPT, ConversationWorker

    c = store.add_conversation_fact("commitment", "The user will send Mira the slides", owner="user", at=T0,
                                    origin="model")
    store.end_conversation_fact(c, "done", at=T0 + 3600, note="done")
    _not_done_yet(store, c)
    line = ConversationWorker._item_line(store.conversation_fact(c), lambda v, n: str(v or "")[:n])
    assert f", put back by the user {datetime.fromtimestamp(BACK):%Y-%m-%d %H:%M}" in line
    assert '"put back by the user"' in EXTRACT_SYSTEM_PROMPT
