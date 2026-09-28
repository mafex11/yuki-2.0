"""The user's own resources (sites, accounts, emails, repos) in conversation memory (migration 22).

Offline: temp databases, scripted model output, synthetic vectors. No network, no real memory.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.conversations import (
    AS_HEARD_NOTE, RECORD_CONVERSATION_TOOL, ConversationWorker, fact_embed_text, resource_seen,
)
from yuki.memory.store import STANDING_KINDS, Store


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


class Turn:
    def __init__(self, tid, user, reply="", actions=(), at=None):
        self.id, self.user_text, self.reply_text, self.actions = tid, user, reply, list(actions)
        self.at = at if at is not None else time.time() - 100 + tid
        self.addressed = "yuki"


# the 2026-09-28 exchange, shortened: misheard, corrected, then the page that opened
TURNS = [
    Turn(1, "Open my portfolio devmyfix dot com.", "Opening your portfolio.", ["open_url (by voice)"]),
    Turn(2, "No it's dev mefex.", "My bad, opening DevMafex now.", ["open_url (by voice)"]),
    Turn(3, "Open devmafex dot com in a new tab", "Portfolio's up.",
         ['open_url: Google Chrome is now showing https://www.devmafex.com/ (page "Kian Thorne")']),
]


def _op(**kw):
    base = {"op": "ADD", "item": 0, "kind": "resource", "subject": "portfolio website", "title": "", "quote": "",
            "due": "", "end_as": "", "card": 0, "owner": "", "reason": "", "keyterms": [], "exchanges": [2, 3]}
    return {**base, **kw}


def test_schema_and_migration_carry_resources(store):
    op = RECORD_CONVERSATION_TOOL["input_schema"]["properties"]["operations"]["items"]
    assert "resource" in op["properties"]["kind"]["enum"] and {"value", "keyterms"} <= set(op["required"])
    assert "resource" in STANDING_KINDS
    fid = store.add_conversation_fact("resource", "The user's portfolio website is devmafex.com.", origin="model",
                                      subject="portfolio website", value="devmafex.com", keyterms=["devmafex"])
    raw = store._query("SELECT value_ciphertext, keyterms_ciphertext FROM conversation_facts WHERE id=?", (fid,))[0]
    assert raw[0] and "devmafex" not in raw[0] and "devmafex" not in raw[1]      # encrypted like the text
    f = store.conversation_fact(fid)
    assert (f.value, f.keyterms) == ("devmafex.com", ["devmafex"])


def test_the_extractor_keeps_the_spelling_that_worked_and_marks_a_dictated_one(store):
    worker = ConversationWorker(store, client=object())
    changes, records = worker.validate([
        _op(text="The user's portfolio website is devmafex.com (https://www.devmafex.com/).", value="devmafex.com",
            keyterms=["devmafex"], quote="No it's dev mefex."),
        _op(subject="email address", text="The user's email address is jane@example.org.", value="jane@example.org",
            exchanges=[1]),
        _op(text="The user's portfolio is somewhere.", value=""),
    ], TURNS, [], {}, set())
    assert [r.status for r in records][:2] == ["applied", "applied"] and records[2].status.startswith("rejected")
    site, mail = changes
    assert site.value == "devmafex.com" and site.keyterms == ["devmafex"] and AS_HEARD_NOTE not in site.text
    assert site.quote == "No it's dev mefex."                      # the user's own words, as provenance
    assert mail.text.endswith(AS_HEARD_NOTE)                        # nothing in the exchanges spells it
    assert resource_seen("devmafex.com", TURNS) and not resource_seen("devmyfixx.com", TURNS)


def test_resources_are_on_every_request_and_found_by_meaning(store):
    vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
    fid = store.add_conversation_fact("resource", "The user's portfolio website is devmafex.com.", origin="model",
                                      subject="portfolio website", value="devmafex.com", keyterms=["devmafex"],
                                      vector=vec, embed_model="test")
    store.add_conversation_fact("resource", f"The user's email is jane@example.org. {AS_HEARD_NOTE}",
                                origin="model", subject="email address", value="jane@example.org",
                                keyterms=["janeexample"])
    m = MemoryClient(store)
    standing = m.standing_context()
    assert "The user's own sites, accounts and links" in standing and f"[resource:{fid}]" in standing
    # fast memory: the resource kind is indexed, never ages, and reads back with its value
    index = m.relevant_index
    index.refresh()
    hits = index.search(vec, now=time.time() + 400 * 86400, min_cosine=0.1)
    assert hits[0]["kind"] == "resource" and hits[0]["id"] == fid
    item = m._relevant_item(hits[0])
    assert item["kind"] == "resource" and item["value"] == "devmafex.com" and "devmafex.com" in item["text"]
    # voice keyterms: a confirmed spelling only (a dictated one could teach recognition a mistake)
    names = m.speech_names()
    assert "devmafex" in names and "janeexample" not in names
    assert [r["confirmed"] for r in m.user_resources()] == [True, False]


def test_a_revoked_resource_leaves_everything(store):
    vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
    fid = store.add_conversation_fact("resource", "The user's portfolio website is devmafex.com.", origin="model",
                                      subject="portfolio website", value="devmafex.com", keyterms=["devmafex"],
                                      vector=vec, embed_model="test")
    m = MemoryClient(store)
    m.relevant_index.refresh()
    assert store.end_conversation_fact(fid, "revoked", note="the user said it is not theirs")
    m.relevant_index.refresh()
    assert m.relevant_index.search(vec, min_cosine=0.1) == []
    assert m.user_resources() == [] and "devmafex" not in m.speech_names()


def test_a_resource_is_embedded_as_what_it_is_and_its_value(store):
    fid = store.add_conversation_fact("resource", "The user's portfolio website is devmafex.com (long sentence).",
                                      origin="model", subject="portfolio website", value="devmafex.com")
    assert fact_embed_text(store.conversation_fact(fid)) == "portfolio website: devmafex.com"
    rid = store.add_conversation_fact("rule", "Call the user babe.", quote="call me captain")
    assert fact_embed_text(store.conversation_fact(rid)) == "Call the user babe. call me captain"
