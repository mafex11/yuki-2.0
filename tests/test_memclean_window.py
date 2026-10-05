"""The window's "Waiting for your yes" on the Me page (report 76 item 5, the review's items not done): what memory holds
back until the user says yes - a claim from a call, someone else's line, a sensitive fact - listed in the private
window (never a corner card), each said to the user with where it came from, and answered at once (``resolve_held``,
no model). Offline: a temp database, invented people (Tara) and facts; the window object is built without connecting.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from yuki.memory import admission as A
from yuki.memory.store import ModelCall, NewFact, Store

T0 = datetime(2026, 10, 4, 23, 0).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def held(store: Store, text: str, reason: str, source: dict, at: float) -> int:
    fact = NewFact(at=at, thread_id=None, app="Discord" if source.get("kind") == "call" else "Google Chrome",
                   host=None, fact=text, importance=5, vector=np.ones(8, dtype=np.float32), embed_model="stub",
                   source=source, held=reason)
    store.commit_batch(ModelCall(at=at, thread_id=None, model="test"), [], [fact])
    return max(h.id for h in A.held_facts(store, since=0, now=at + 1, answered=True))


def test_waiting_for_your_yes_lists_call_claims_others_lines_and_sensitive_facts_said_to_the_user(store, tmp_path):
    import time

    from yuki.memory.api import MemoryClient
    from yuki.ui import web_data, web_window

    now = time.time()
    call = {"kind": "call", "by": "the user", "where": "Discord call", "at": now - 3600, "call": "call:3"}
    japan = held(store, "The user has been to Japan four times.", A.HOLD_CALL,
                 {**call, A.SHOWN_KEY: "You've been to Japan four times."}, now - 3600)
    trek = held(store, "Someone on the call said the trek starts at 6 AM.", A.HOLD_CALL,
                {**call, "who": "others", "by": "someone on the call",
                 A.SHOWN_KEY: "Someone on the call said the trek starts at 6 AM."}, now - 3500)
    money = held(store, "The user checked the holdings in their Northbank brokerage account.", A.HOLD_SENSITIVE,
                 {"kind": "page", "where": "Northbank - Holdings", "at": now - 600,
                  A.SHOWN_KEY: "You checked your Northbank holdings."}, now - 600)
    held(store, "The user has five girlfriends.", A.HOLD_DROP, dict(call), now - 3400)   # dropped: never listed
    mem = MemoryClient(store)
    view = web_data.held_view(mem, now)
    assert view["direct"] is True
    by_ref = {i["ref"]: i for i in view["items"]}
    assert list(by_ref) == [f"journal:{money}", f"journal:{trek}", f"journal:{japan}"]   # newest first
    assert by_ref[f"journal:{money}"]["text"] == "You checked your Northbank holdings."
    assert by_ref[f"journal:{money}"]["sensitive"] is True and by_ref[f"journal:{money}"]["prov"].startswith(
        "Seen in Northbank")
    assert by_ref[f"journal:{trek}"]["others"] is True and by_ref[f"journal:{japan}"]["others"] is False
    assert by_ref[f"journal:{japan}"]["prov"].startswith("Heard on a Discord call")
    assert not any("The user" in i["text"] for i in view["items"])
    assert web_data.held_view(object(), now) is None          # an older memory: no list

    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = mem
    win.refresh = lambda *a, **k: None
    assert win.a_resolve_held({"ref": f"journal:{money}", "verdict": "keep"}) == {"correction_id": None,
                                                                                    "verdict": "keep"}
    assert [e.fact for e in store.journal_between()] == [
        "The user checked the holdings in their Northbank brokerage account."]   # his explicit yes: in memory now
    assert win.a_resolve_held({"ref": f"journal:{money}", "verdict": "keep"})["ok"] is False   # already answered
    assert win.a_resolve_held({"ref": f"journal:{money}", "verdict": "maybe"})["ok"] is False
    assert win.a_resolve_held({"ref": f"journal:{money}", "verdict": "undo"})["verdict"] == "undo"
    assert store.journal_between() == []
    assert "resolve_held" in web_window.WebWindow.ACTIONS
    me_js = Path(web_window.__file__).with_name("web") / "pages" / "me.js"
    assert "Waiting for your yes" in me_js.read_text(encoding="utf-8")
