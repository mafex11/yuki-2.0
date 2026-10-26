"""Typed offers (yuki.memory.offers; the design notes §4-5): validated when the
card is written and again at the click, run by code (never an agent, never a model call), with Undo, and learned
from per (channel, kind) with numbers. Temp databases only; nothing touches the desktop or a model.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from yuki.memory import offers
from yuki.memory.api import MemoryClient
from yuki.memory.store import NewNudge, NudgeCheckin, Store, WorkOp


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def thread(store: Store, title: str = "Operator demo", open_items=("build the booth link cache",)) -> int:
    op = WorkOp("starts", None, title=title, state="booth links half done", open_items=list(open_items),
                at=time.time() - 3600)
    store.apply_work([op])
    return op.result_id


def link(store: Store, url: str, title: str, work_thread_id: int | None = None, by_yuki: bool = False) -> int:
    now = time.time()
    return store.upsert_links([{"url": url, "how": "viewed", "at": now - 600, "until": now - 300, "title": title,
                                "host": url.split("/")[2], "work_thread_id": work_thread_id, "by_yuki": by_yuki}])[0]


def card(store: Store, offer: dict | None, *, at: float | None = None, kind: str = "notice",
         trigger: str = "thread") -> int:
    at = time.time() if at is None else at
    _cid, nid = store.record_checkin(NudgeCheckin(at=at, trigger=trigger, outcome=kind, model="m"),
                                     NewNudge(kind=kind, text="A card.", trigger=trigger, inputs={"offer": offer}))
    return nid


def valid(store: Store, raw: dict, ids=()) -> dict:
    offer, why = offers.validate_offer(raw, ids=set(ids), store=store)
    assert offer is not None, why
    return offer


TODO = {"do": "todo_add", "label": "Add to list", "steps": ["add 'Build the booth link cache', due Monday"],
        "refs": [], "text": "Build the booth link cache", "due": "2026-10-05"}


# -- validated when the card is written ---------------------------------------------------------------------


def test_an_offer_must_be_a_known_kind_with_steps_offered_ids_and_what_its_kind_needs(store):
    wid = thread(store)
    check = lambda raw, ids=(): offers.validate_offer(raw, ids=set(ids), store=store)[1]   # noqa: E731
    assert check(None) == "none" and check({"do": "none"}) == "none"
    assert check("Open T3 Code and run the import") == "not_an_object"      # a sentence is not an offer
    assert check({**TODO, "do": "run_command"}) == "unknown_do"              # the closed list
    assert check({**TODO, "steps": []}) == "no_steps"
    assert check({**TODO, "refs": ["W999"]}) == "unknown_ref"               # an id the request did not offer
    assert check({**TODO, "text": ""}) == "no_text"
    assert check({**TODO, "due": "next week"}) == "bad_due"
    assert check({"do": "show_memory", "label": "Show", "steps": ["show it"], "refs": ["W999"], "text": "",
                  "due": ""}, ids={"W999"}) == "unresolved_ref"              # names nothing Yuki holds
    assert check({"do": "bring_back", "label": "Open", "steps": ["open it"], "refs": [f"W{wid}"], "text": "",
                  "due": ""}, ids={f"W{wid}"}) == "unresolved_ref"           # the thread has no page to open
    assert offers.validate_offer(TODO, ids=set(), store=store, allowed_do=("draft",))[1] == "not_allowed"
    offer = valid(store, {**TODO, "label": "a label far too long to sit on one small button"})
    assert offer["label"] == "Add to list" and offer["due"] == "2026-10-05"   # the kind's own label instead


def test_bring_back_resolves_to_a_page_the_user_had_and_only_a_web_page(store):
    wid = thread(store)
    lid = link(store, "https://docs.example.com/booth", "Booth link sheet", work_thread_id=wid)
    bad = link(store, "file:///C:/Users/yukidev/secret.txt", "secret")
    offer = valid(store, {"do": "bring_back", "label": "Open it again", "steps": ["open the booth sheet again"],
                          "refs": [f"W{wid}"], "text": "", "due": ""}, ids={f"W{wid}"})
    assert offer["target"]["url"] == "https://docs.example.com/booth" and offer["target"]["title"] == "Booth link sheet"
    assert valid(store, {**offer, "refs": [f"L{lid}"]}, ids={f"L{lid}"})["target"]["url"].startswith("https://")
    assert offers.validate_offer({**offer, "refs": [f"L{bad}"]}, ids={f"L{bad}"}, store=store)[1] == "unresolved_ref"


# -- checked again at the click, run by code -------------------------------------------------------------------


def test_todo_add_then_undo_leaves_the_list_as_it_was(store):
    client = MemoryClient(store)
    before = client.todos()
    nid = card(store, valid(store, TODO))
    result = client.offer_act(nid)
    assert result["ok"] and result["state"] == "done" and result["undo"]
    assert [t["text"] for t in client.todos()] == [*[t["text"] for t in before], "Build the booth link cache"]
    assert store.nudge(nid).outcome == "done"
    undone = client.offer_undo(nid)
    assert undone["ok"] and client.todos() == before and store.nudge(nid).outcome == "undone"
    assert client.offer_undo(nid)["ok"] is False                        # once


def test_a_second_click_an_expired_offer_or_a_gone_ref_runs_nothing(store):
    client = MemoryClient(store)
    nid = card(store, valid(store, TODO))
    assert client.offer_act(nid)["ok"]
    again = client.offer_act(nid)
    assert again == {"ok": False, "why": "already", "state": "done", "do": "todo_add"} and len(client.todos()) == 1
    old = card(store, valid(store, TODO), at=time.time() - offers.OFFER_TTL_S - 5)
    assert client.offer_act(old)["why"] == "expired" and len(client.todos()) == 1
    assert store.nudge(old).outcome == "expired"
    wid = thread(store, title="Wallpaper Engine")
    shown = card(store, valid(store, {"do": "show_memory", "label": "Show", "steps": ["show where it stands"],
                                     "refs": [f"W{wid}"], "text": "", "due": ""}, ids={f"W{wid}"}))
    store.change_work_threads([{"action": "dismiss", "id": wid, "reason": "test"}])
    assert client.offer_act(shown)["why"] == "gone"                     # re-resolved at the click
    assert client.offer_act(card(store, {"do": "todo_add"}))["why"] == "no_offer"   # not a valid stored offer
    assert client.offer_act(99999)["why"] == "unknown"


def test_offers_off_runs_nothing(store, monkeypatch):
    from yuki.memory.nudges import NudgeConfig

    client = MemoryClient(store)
    monkeypatch.setattr(client, "_nudges_config", lambda: NudgeConfig.from_dict({"offers": "off"}))
    nid = card(store, valid(store, TODO))
    assert client.offer_act(nid)["why"] == "off" and client.todos() == []
    assert offers.allowed_dos(store, "thread", enabled=False) == ()


def test_show_memory_and_ask_return_what_the_card_shows_and_a_draft_is_never_offered(store):
    client = MemoryClient(store)
    wid = thread(store)
    nid = card(store, valid(store, {"do": "show_memory", "label": "Show", "steps": ["show where it stands"],
                                   "refs": [f"W{wid}"], "text": "", "due": ""}, ids={f"W{wid}"}))
    result = client.offer_act(nid)
    assert result["ok"] and result["items"][0]["title"] == "Operator demo" and "booth" in result["items"][0]["text"]
    # r30: jSwitch (10 Oct) retired the draft offer (RETIRED_DOS): a reply to copy is the user's own work
    assert offers.validate_offer({"do": "draft", "label": "Show draft", "steps": ["show a reply to Kian"], "refs": [],
                                  "text": "Hey Kian,\nthe PR is merged.", "due": ""}, ids=set(), store=store) == \
        (None, "not_allowed")
    ask = card(store, valid(store, {"do": "ask", "label": "Edit in ask bar", "steps": ["put it in the ask bar"],
                                   "refs": [], "text": "Check the T3 Code agent's stream failure", "due": ""}))
    assert client.offer_act(ask)["state"] == "prefilled" and store.nudge(ask).outcome == "prefilled"
    assert client.offer_outcome(ask, "stopped") and store.nudge(ask).outcome == "stopped"
    assert not client.offer_outcome(ask, "done")                         # only a prefilled card moves


def test_bring_back_returns_the_page_and_undo_is_recorded(store):
    client = MemoryClient(store)
    lid = link(store, "https://docs.example.com/booth", "Booth link sheet")
    nid = card(store, valid(store, {"do": "bring_back", "label": "Open it again", "steps": ["open the sheet again"],
                                   "refs": [f"L{lid}"], "text": "", "due": ""}, ids={f"L{lid}"}))
    result = client.offer_act(nid)
    assert result["ok"] and result["url"] == "https://docs.example.com/booth" and result["undo"]
    assert client.offer_undo(nid)["ok"] and store.nudge(nid).outcome == "undone"


def test_the_card_row_carries_the_typed_offer_its_wont_line_and_expiry(store):
    nid = card(store, valid(store, TODO))
    row = MemoryClient(store).pending_nudges()[0]
    assert row["id"] == nid and row["offer"]["do"] == "todo_add" and row["offer"]["undo"] is True
    assert row["offer"]["wont"] == offers.WONT_LINES["todo_add"] and row["offer"]["expires_at"]
    assert row["offer"]["steps"] == TODO["steps"] and row["outcome"] is None


# -- learning: numbers per (channel, kind) -------------------------------------------------------------------


def test_three_not_nows_on_one_kind_remove_that_button_but_not_the_channel(store):
    # r30: presence.channel_budgets went with the old card writers (kProactive, 18 Oct); the offer-score checks stay
    lid = link(store, "https://docs.example.com/booth", "Booth link sheet")
    back = valid(store, {"do": "bring_back", "label": "Open it again", "steps": ["open it again"],
                         "refs": [f"L{lid}"], "text": "", "due": ""}, ids={f"L{lid}"})
    now = time.time()
    for k in range(6):   # 6 x Not now = -3.0: at the line
        nid = card(store, back, at=now - 3600 * (k + 1))
        store.react_to_nudge(nid, "dismissed", at=now - 3600 * (k + 1) + 30)
    assert offers.offer_scores(store, now)[("thread", "bring_back")] == -3.0
    assert "bring_back" not in offers.allowed_dos(store, "thread", now)
    assert "todo_add" in offers.allowed_dos(store, "thread", now)        # only that kind
    assert "bring_back" in offers.allowed_dos(store, "recall", now)      # only on that channel
    lines = offers.feedback_lines(store, lambda v, n: str(v)[:n], now)
    assert any("bring_back offers on thread cards" in ln and "no more such offers" in ln for ln in lines)


def test_one_press_then_stop_counts_against_it_never_as_useful(store):
    from yuki.memory.presence import card_feedback_lines

    ask = valid(store, {"do": "ask", "label": "Edit in ask bar", "steps": ["put it in the ask bar"], "refs": [],
                        "text": "Look into the failing deploy", "due": ""})
    nid = card(store, ask, trigger="stuck")
    store.react_to_nudge(nid, "accepted")
    store.set_nudge_outcome(nid, "prefilled")
    offers.record_outcome(store, nid, "stopped")
    assert offers.offer_scores(store)[("stuck", "ask")] == -1.0
    lines = card_feedback_lines(store, lambda v, n: str(v)[:n])
    assert any("stopped it within a minute: not wanted" in ln for ln in lines)
    assert not any("useful" in ln for ln in lines)
    kept = card(store, valid(store, TODO), at=time.time() - 2 * offers.UNDO_WITHIN_S, trigger="stuck")
    store.set_nudge_outcome(kept, "done", at=time.time() - offers.UNDO_WITHIN_S - 5)
    assert offers.offer_scores(store)[("stuck", "todo_add")] == 1.0     # ran and kept past the undo time: +1


# -- Yuki's own words are never the user's (conversation fact 41, 1 Oct) -----------------------------------------


def test_a_do_it_request_never_becomes_the_users_feedback(store):
    from yuki.memory.conversations import ConversationWorker, has_card_context, own_words

    offer = "Find yuki-2.0 in T3 Code's project selector, tick it, and run the import."
    now = time.time()
    _cid, nid = store.record_checkin(NudgeCheckin(at=now - 30, trigger="stuck", outcome="notice", model="m"),
                                     NewNudge(kind="notice", text="Stuck on the import.", trigger="stuck"))
    store.react_to_nudge(nid, "accepted", at=now - 10)
    text = (f'[The user pressed "Do it" on your card (kind: notice), sent 03:09: "Stuck on the import." The line '
            f"below is their own instruction, as if they had typed it.]\n{offer}")
    turn_id = store.add_turn("s1", 1, now, text)
    store.flush_turns()
    assert own_words(text) == "" and not has_card_context(text)
    suggestion = f"[Yuki's own suggestion from its card N{nid}, sent unchanged by the user]\nCheck the run"
    assert own_words(suggestion) == "" and not has_card_context(suggestion)
    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids([turn_id])
    items = [{"op": "ADD", "item": 0, "kind": "feedback", "subject": "cards", "text": "", "quote": offer,
              "due": "", "end_as": "", "card": nid, "exchanges": [1], "reason": "r"}]
    changes, records = worker.validate(items, turns, [], worker.cards_for(turns))
    assert not changes and records[0].status.startswith("rejected")
    message = worker.build_extract_message(turns, [], [], [], [], now, cards=worker.cards_for(turns))
    assert "YUKI'S OWN WORDS" in message
