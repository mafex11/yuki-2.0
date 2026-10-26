"""Offline checks of the observation cards (yuki.memory.presence): the measured signals, the attention budgets and
the API fields the cards need. The old look-and-write chain (observe, Look, channel_budgets, the presence worker's
looks and budgets) went with kProactive (18 Oct), and its tests with it: the card judge writes every card now.

Temp databases, synthetic data and stub models only; nothing touches the desktop or a real model.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yuki.core.attention import DEFAULT_RULES, PRESENCE_CHANNELS, Attention
from yuki.memory.api import MemoryClient
from yuki.memory.presence import _call_sessions, recurrence
from yuki.memory.store import NewNudge, NudgeCheckin, Store

DAY = datetime(2026, 9, 28)


def at(hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return (DAY + timedelta(hours=h, minutes=m)).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def fact(store: Store, when: float, text: str, vec: np.ndarray | None = None, **kw) -> int:
    fid = store.add_journal(when, None, kw.pop("app", "Google Chrome"), text, kw.pop("importance", 6),
                            vector=vec, embed_model="test" if vec is not None else None, **kw)
    with store._lock:   # memory learned it when it happened (a live journal writes within minutes)
        store._conn.execute("UPDATE journal SET created_at=? WHERE id=?", (when + 60.0, fid))
    return fid


def unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


#: A typed offer as presence stores it (yuki.memory.offers): a closed kind, its steps, run by code.
TODO_OFFER = {"do": "todo_add", "label": "Add to list", "steps": ["add 'Check the deploy' to your list"], "refs": [],
              "text": "Check the deploy", "due": None}


def card(store: Store, when: float, text: str, trigger: str, reaction: str | None = None,
         kind: str = "notice", outcome: str | None = None, offer: dict | None = TODO_OFFER) -> int:
    _cid, nid = store.record_checkin(NudgeCheckin(at=when, trigger=trigger, outcome=kind, model="m"),
                                     NewNudge(kind=kind, text=text, trigger=trigger,
                                              inputs={"offer": dict(offer) if offer else None}))
    if reaction:
        store.react_to_nudge(nid, reaction, at=when + 20)
    if outcome:
        store.set_nudge_outcome(nid, outcome, at=when + 25)
    return nid


# -- the measured signals ---------------------------------------------------------------------------


def test_recurrence_needs_similar_fresh_facts_over_minutes():
    F = SimpleNamespace
    same = [F(id=1, at=at("19:52")), F(id=2, at=at("20:27")), F(id=3, at=at("20:46"))]
    other = [F(id=4, at=at("20:40"))]
    vecs = {1: unit(1, 0.05, 0), 2: unit(1, 0.1, 0), 3: unit(1, 0, 0.1), 4: unit(0, 1, 0)}
    groups = recurrence(same + other, vecs, now=at("20:50"))
    assert [[f.id for f in g] for g in groups] == [[1, 2, 3]]
    # too close together (a burst, not something coming back), or no longer fresh: nothing
    burst = [F(id=1, at=at("20:45")), F(id=2, at=at("20:46")), F(id=3, at=at("20:47"))]
    assert recurrence(burst, vecs, now=at("20:50")) == []
    assert recurrence(same, vecs, now=at("21:30")) == []
    # below the cosine it is not the same thing
    far = {1: unit(1, 0, 0), 2: unit(0.6, 0.8, 0), 3: unit(0.6, 0, 0.8)}
    assert recurrence(same, far, now=at("20:50")) == []


def test_call_rows_close_together_are_one_session():
    C = SimpleNamespace
    rows = [C(id=34, app="Discord", detected_at=at("21:37"), ended_at=at("21:57")),
            C(id=35, app="Discord", detected_at=at("22:01"), ended_at=at("23:00")),
            C(id=36, app="Discord", detected_at=at("23:06"), ended_at=at("23:59")),
            C(id=40, app="Zen", detected_at=at("11:00"), ended_at=at("11:05")),
            C(id=41, app="Discord", detected_at=at("09:00"), ended_at=None)]
    sessions = _call_sessions(rows)
    assert [[c.id for c in s] for s in sessions] == [[40], [34, 35, 36]]


# -- the attention budgets ---------------------------------------------------------------------------


def test_attention_history_comes_from_the_stored_cards_and_spreads_them():
    now = [at("12:00")]
    att = Attention(clock=lambda: now[0])
    assert set(PRESENCE_CHANNELS) <= set(DEFAULT_RULES)
    att.load("presence", [at("11:40")])
    d = att.check("presence", now=now[0], facts={})
    assert not d.allowed and d.reason == "cooldown"                     # 25 minutes apart
    att.load("presence", [at("11:05"), at("11:30")])
    assert att.check("presence", now=now[0], facts={}).reason == "budget"   # two an hour
    att.load("stuck", [at("09:00"), at("10:00"), at("11:00")])
    assert att.check("stuck", now=now[0], facts={}).reason == "budget"


# -- the cards' offers and history ------------------------------------------------------------------


def test_a_to_do_offer_for_an_item_the_list_has_is_refused(store):
    from yuki.memory import offers

    item = {"do": "todo_add", "label": "Add to list", "steps": ["add the emails to your list"],
            "refs": ["commit:35"], "text": "Send 15 emails", "due": ""}
    assert offers.validate_offer(item, ids={"commit:35"}, store=store) == (None, "already_listed")


def test_a_missed_card_stays_in_the_history_and_is_never_pending(store):
    nid = card(store, at("10:00"), "Something", "recall")
    store.expire_nudges(at("10:31"), ("notice",))
    client = MemoryClient(store)
    assert all(n["id"] != nid for n in client.pending_nudges())
    import time as _time

    rows = client.recent_nudges(hours=(_time.time() - at("09:00")) / 3600.0)
    missed = [r for r in rows if r["id"] == nid]
    assert missed and missed[0].get("missed") and missed[0]["offer"]["do"] == "todo_add"
    assert missed[0]["offer"]["label"] == "Add to list" and missed[0]["outcome"] is None


def test_a_free_text_offer_from_before_the_change_gets_no_button(store):
    import json as _json

    nid = card(store, at("10:00"), "Something", "stuck", offer=None)
    n = store.nudge(nid)
    inputs = {**n.inputs, "offer": "Find yuki-2.0 in T3 Code's project selector, tick it, and run the import."}
    with store._lock:   # as a card written before 3 Oct kept it: one sentence for the agent
        store._conn.execute("UPDATE nudges SET inputs_ciphertext=? WHERE id=?",
                            (store.cipher.encrypt(_json.dumps(inputs)), nid))
    assert MemoryClient._nudge_dict(store.nudge(nid))["offer"] is None
