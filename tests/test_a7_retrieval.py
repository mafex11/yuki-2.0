"""Revamp A6 P0.1 and A7 (docs/research/revamp-2026-10-02/22-build-A6-A9.md): the standing context as parts, memory
searched with the user's own words, Yuki's own conversation never returned as memory, the injection diet (to-dos and
know-how only when they bear on the words), and turns stored apart from Yuki's envelope.

Temp databases and a stub embedder (a text's first word "aN" is the axis it embeds on); no model, no network.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from yuki.agent.memory import (
    MemoryAccess,
    memory_block_text,
    memory_query_of,
    memory_words,
    split_standing,
    standing_commitments_text,
    standing_rules_text,
)
from yuki.agent.prompt import NO_RULES, rules_text
from yuki.memory.api import STANDING_HEADINGS, MemoryClient
from yuki.memory.store import Store, split_envelope

DIM = 8


def unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


class StubEmbedder:
    """A text embeds on the axis its first word names ("a3 ..." -> e3; anything else e7)."""

    model_name = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts, wait_s=None):
        self.calls += 1
        out = []
        for t in texts:
            head = (str(t).split() or ["x"])[0]
            i = int(head[1]) if len(head) == 2 and head[0] == "a" and head[1].isdigit() else 7
            out.append(unit(i))
        return np.stack(out)


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


@pytest.fixture()
def client(store):
    c = MemoryClient(store)
    c._embedder = StubEmbedder()
    return c


# -- A6 P0.1: the standing context as parts -------------------------------------------------------------------


def test_todos_never_pose_as_rules_for_the_agent_or_the_talker(store, client):
    """09 §3.4: with no active rule, the whole context (six to-dos) became the block "every one applies"."""
    store.add_conversation_fact("commitment", "Send Varun the updated deck on Friday", quote="I'll send the deck",
                                origin="call", owner="user", subject="call (Zoom): deck")
    store.add_conversation_fact("commitment", "Remind the user to stretch at 8 pm", origin="model", owner="yuki")
    store.add_conversation_fact("resource", "The user's portfolio website is janedoe.dev", value="janedoe.dev",
                                subject="portfolio website")
    parts = client.standing_parts()
    assert [i["ref"] for i in parts["groups"]["todo"]] and not parts["groups"]["rule"]
    text = client.standing_context()                      # the old string, same shape
    rules, rest = split_standing(text)
    assert rules is None and "Send Varun the updated deck" in rest and "Remind the user" in rest
    from yuki.voice.mouth import mouth_rules             # the talker reads the string, unchanged

    talker = mouth_rules(text)
    assert "Send Varun" not in talker and "Remind the user" not in talker and "janedoe.dev" in talker

    ctx = MemoryAccess(open_client=lambda: client).context(app=None, query="", resume=False)
    assert standing_rules_text(ctx) == ""                 # the rules layer says "no rules", nothing else
    assert NO_RULES in rules_text(standing_rules_text(ctx))
    block = standing_commitments_text(ctx)
    assert STANDING_HEADINGS["todo"] in block and "Send Varun the updated deck" in block
    assert STANDING_HEADINGS["commitment"] in block and "Remind the user" in block
    assert not block.startswith("[Standing rules")        # memory's own header is not repeated in the block


def test_rules_stay_in_the_rules_layer_and_only_there(store, client):
    store.add_conversation_fact("rule", "Call me captain", quote="call me captain always")
    store.add_conversation_fact("commitment", "a1 send the invoice", origin="call", owner="user")
    ctx = MemoryAccess(open_client=lambda: client).context(app=None, query="", resume=False)
    assert "call me captain always" in standing_rules_text(ctx) and "invoice" not in standing_rules_text(ctx)
    assert "Call me captain" not in (standing_commitments_text(ctx) or "")


def test_an_unknown_format_is_still_all_rules_so_no_rule_drops_out():
    rules, rest = split_standing("[header]\nSome new heading:\n- a rule in a format the agent does not know")
    assert rules and "a rule in a format" in rules and rest is None


def test_the_agent_side_headings_match_memorys_own():
    from yuki.agent.memory import STANDING_SECTION_HEADINGS

    assert set(STANDING_HEADINGS.values()) == set(STANDING_SECTION_HEADINGS)


# -- A7: memory searched with the user's own words ----------------------------------------------------------


def test_memory_query_is_the_users_words_not_the_voice_envelope():
    from yuki.voice.mouth import brain_request

    sent = brain_request("Restore the pages.", ["uh restore the pages please"], done_before="- click Restore ok",
                         said=["On it."], tabs="Chrome: 3 tabs")
    assert len(sent) > 1500                               # the envelope the embedder used to read
    query = memory_query_of(sent)
    assert query.startswith("Restore the pages.") and "uh restore the pages please" in query
    assert "say_now" not in query and "Done on the desktop" not in query and len(query) <= 500
    assert memory_query_of("open my portfolio") == "open my portfolio"
    card = "[Replying to your nudge (kind: question): Was that offer real?]\nno it was a joke"
    assert memory_query_of(card).startswith("no it was a joke") and "offer real" in memory_query_of(card)
    assert memory_words("Tag Hemanth", ["tag hey man in a message"]) == "Tag Hemanth | tag hey man in a message"
    assert memory_words("Tag Hemanth", ["tag hemanth"]) == "Tag Hemanth"   # the heard words add nothing new


class _Recorder(MemoryAccess):
    """A memory that records what each request asked it (its words, the previous words, the screen)."""

    def __init__(self) -> None:
        super().__init__(open_client=lambda: SimpleNamespace())
        self.asked: list[dict[str, Any]] = []

    def context(self, **kwargs: Any):  # type: ignore[override]
        from yuki.agent.memory import MemoryContext

        self.asked.append(kwargs)
        return MemoryContext(source="empty")


def test_agent_searches_memory_with_the_words_and_the_previous_words(tmp_path):
    from yuki.agent.loop import Agent
    from yuki.config import Settings
    from yuki.log.events import SessionLogger
    from yuki.voice.mouth import brain_request
    from yuki.voice.offline import StubDesktop

    def response(*blocks):
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage, id="m", model="m",
                               role="assistant", model_dump=lambda **_: {})

    done = SimpleNamespace(type="tool_use", id="d", name="done", input={"message": "Done.", "about": []})
    calls = [response(done), response(done), response(done)]
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: calls.pop(0)))
    memory = _Recorder()
    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)
    list(agent.run(brain_request("Restore the pages.", ["restore the pages"], said=["On it."])))
    list(agent.run("what did I do today?", memory_query="what did I do today"))
    list(agent.run("and yesterday?"))
    first, second, third = memory.asked
    assert first["query"] == "Restore the pages." and first["previous"] is None
    assert second["query"] == "what did I do today" and second["previous"] == "Restore the pages."
    assert third["previous"] == "what did I do today"


def test_the_live_window_in_front_anchors_the_lookup(tmp_path):
    """02 §3.3: "On their screen" named the wrong window (the service's last capture lags a fresh switch)."""
    from yuki.agent.loop import Agent
    from yuki.config import Settings
    from yuki.log.events import SessionLogger
    from yuki.voice.offline import StubDesktop

    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    done = SimpleNamespace(type="tool_use", id="d", name="done", input={"message": "Done.", "about": []})
    reply = SimpleNamespace(content=[done], stop_reason="tool_use", usage=usage, id="m", model="m",
                            role="assistant", model_dump=lambda **_: {})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: reply))
    memory = _Recorder()
    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)
    agent.dispatcher.last_now_board = {"foreground": 0x1001, "windows": [
        {"hwnd": 0x1001, "app": "Google Chrome", "process": "chrome", "live_title": "Restore pages? - Chrome",
         "url": "https://example.com/a", "kind": "page", "text": "Chrome didn't shut down correctly. Restore",
         "read_at": 1.0},
        {"hwnd": 0x1002, "app": "Spotify", "title": "Spotify", "text": "", "read_at": 1.0}]}
    list(agent.run("restore the pages"))
    screen = memory.asked[0]["screen"]
    assert screen["title"] == "Restore pages? - Chrome" and screen["host"] == "example.com"
    assert "Restore" in screen["text"] and screen["source"] == "now"
    agent.dispatcher.last_now_board = {"foreground": 0x1001, "windows": [
        {"hwnd": 0x1001, "app": "Bank", "blocked": "blocked_host", "read_at": 1.0}]}
    list(agent.run("and now?"))
    assert memory.asked[1]["screen"] is None              # a window the privacy rules block anchors nothing


# -- A7: Yuki's own conversation is not memory ----------------------------------------------------------------


def _turn(store: Store, session: str, at: float, words: str, vec: np.ndarray | None = None) -> int:
    tid = store.add_turn(session, 1, at, words, "Sure.", ["click ok"], "final")
    store.flush_turns()
    store.add_conversation_vectors("turn", [(tid, unit(1) if vec is None else vec)], "stub")
    return tid


def test_related_leaves_out_this_conversation_and_the_last_half_hour_but_keeps_older_precedents(store, client):
    now = time.time()
    older = _turn(store, "s0", now - 3 * 86400, "a1 restore the pages")
    near = unit(1) * 0.8 + unit(2) * 0.6      # like it (cosine 0.8), not the same exchange
    gap = _turn(store, "s1", now - 3 * 3600, "a1 an exchange of this app run, before a long pause", near)
    this = _turn(store, "s1", now - 120, "a1 restore the pages")
    recent = _turn(store, "s2", now - 600, "a1 restore the pages")
    found = client.related("a1 restore the pages", session_id="s1", now=now, with_recent=False, embed_wait_s=1.0)
    ids = {i["id"] for i in found["items"] if i["kind"] == "chat"}
    assert older in ids and gap in ids                    # earlier conversations stay (real precedents)
    assert this not in ids and recent not in ids          # the conversation going on, and the last 30 min
    assert found["self_echo"] == 2
    # the talker (no session): the time measure alone
    voice = client.related("a1 restore the pages", now=now, with_recent=False, voice=True, embed_wait_s=1.0)
    assert this not in {i["id"] for i in voice["items"]} and recent not in {i["id"] for i in voice["items"]}


# -- A7: the injection diet ----------------------------------------------------------------------------------


def test_todos_come_only_when_they_bear_on_the_words_and_the_rest_are_counted(store, client):
    store.add_todo("a1 send the invoice to Varun")
    store.add_todo("a2 book the dentist")
    store.add_todo("a3 restart the PC")
    store.add_conversation_fact("commitment", "a2 call the bank about the card", origin="call", owner="user")
    access = MemoryAccess(open_client=lambda: client)
    ctx = access.context(app=None, query="a1 invoice for Varun", resume=False)
    assert [r["text"] for r in ctx.todos] == ["a1 send the invoice to Varun"]
    assert ctx.todos_held == 3                            # two list items and the call to-do, not near the words
    assert "call the bank" not in (standing_commitments_text(ctx) or "")
    text = memory_block_text(ctx)
    assert "a1 send the invoice" in text and "3 more open to-dos" in text and "todo tool" in text
    # no words (the voice mouth's own read): nothing is gated, the old list comes back
    plain = access.context(app=None, query="", resume=False)
    assert len(plain.todos) == 3 and plain.todos_held == 0


def test_knowhow_by_relevance_with_a_lower_floor_for_the_apps_in_play(store, client):
    store.add_knowhow("discord", "a1 mention by typing @ and the name, then Tab", vector=unit(1), embed_model="stub")
    store.add_knowhow("warp", "a5 restore a saved tab with ctrl+shift+t", vector=unit(5), embed_model="stub")
    v = (unit(1) * 0.35 + unit(2) * 0.94)
    store.add_knowhow("discord", "a2 server list on the left", vector=v / np.linalg.norm(v), embed_model="stub")
    near = client.knowhow("warp.exe", "a1 tag Rohan in the channel", 6, 1.0, min_cosine=0.4, apps=["Discord.exe"],
                          app_min_cosine=0.3)
    assert near[0].startswith("[discord] a1 mention")
    assert any("server list" in x for x in near)          # cosine 0.35: in only because Discord is in play
    assert not any("warp" in x for x in near)             # the app in front, but nothing near the words
    alone = client.knowhow("warp.exe", "a1 tag Rohan in the channel", 6, 1.0, min_cosine=0.4, app_min_cosine=0.3)
    assert not any("server list" in x for x in alone)
    # no vector for the words (the embedder is down): only the apps in play, newest first
    client._embedder = SimpleNamespace(embed=lambda texts, wait_s=None: None)
    client._query_vecs.clear()
    fallback = client.knowhow("discord.exe", "something new", 6, 0.1, min_cosine=0.4)
    assert fallback and all(x.startswith("[discord]") for x in fallback)


def test_threads_and_recent_screens_only_as_far_as_they_bear_on_the_words(store, client):
    from yuki.memory import related as rel
    from yuki.memory.store import WorkOp

    now = time.time()
    store.apply_work([WorkOp("starts", None, title="a1 invoice for Varun", at=now - 600, origin="journal"),
                      WorkOp("starts", None, title="a4 cosplay costume", at=now - 300, origin="journal")])
    for t in store.work_threads(("active",)):
        store._write(lambda conn, t=t: store._put_work_vec(conn, t.id, unit(int(t.title[1])), "stub"))
    found = client.related("a1 the invoice", now=now, with_recent=False, diet=True, embed_wait_s=1.0)
    titles = [t.title for t in found["threads"]]
    assert titles == ["a1 invoice for Varun"] and found["diet"] is True
    whole = client.related("a1 the invoice", now=now, with_recent=False, embed_wait_s=1.0)
    assert len(whole["threads"]) == 2                     # without the diet (talker, MCP): as before
    # recent screens: the last minutes whole; an older window only when its journal bears on the words
    tid_a = store.upsert_thread("Chrome", "Invoice - Gmail", "https://mail.example/inv", process="chrome.exe")
    tid_b = store.upsert_thread("Chrome", "Feed", "https://feed.example/", process="chrome.exe")
    tid_c = store.upsert_thread("Discord", "#general", None, process="discord.exe")
    store.add_capture(tid_a, now - 1200, "focus", "a1 invoice draft for Varun", "a1 invoice draft for Varun", kind="page")
    store.add_capture(tid_b, now - 900, "focus", "cat videos", "cat videos", kind="page")
    store.add_capture(tid_c, now - 30, "focus", "lol", "lol", kind="page")
    jid = store.add_journal(now - 1100, tid_a, "Chrome", "a1 the user drafted the invoice for Varun", 6,
                            vector=unit(1), embed_model="stub")
    assert jid
    store._write(lambda conn: conn.execute("UPDATE journal SET created_at=? WHERE id=?", (now - 1100, jid)))
    client.relevant_index.refresh()
    groups = rel.recent(store, now=now, select=lambda g: rel._diet_recent(client, g, unit(1), now=now,
                                                                           embed_wait_s=None))
    starts = sorted(int(g["start"]) for g in groups)
    assert int(now - 1200) in starts and int(now - 30) in starts and int(now - 900) not in starts


# -- A7: turns stored apart from Yuki's envelope ---------------------------------------------------------------


def test_turn_words_and_envelope_are_stored_apart_and_recall_shows_what_yuki_did(store, client):
    from yuki.voice.mouth import brain_request

    sent = brain_request("Restore the pages.", ["restore the pages"], done_before="- click ok", said=["On it."])
    tid = client.log_turn("s", 1, time.time(), sent, "Done, they're back.", ["click Restore ok"], "final")
    store.flush_turns()
    (t,) = store.turns_by_ids([tid])
    assert t.user_text == "Restore the pages." and t.envelope and t.full_text == sent
    line = client._chat_text(t)
    assert line == ('The user said: "Restore the pages." Yuki replied: "Done, they\'re back." '
                    'Yuki did: click Restore ok.')
    assert Store.turn_text(t).startswith("Restore the pages.\nDone")   # what the turn's vector is made from
    # a turn stored before migration 27 (words and envelope in one field) reads the same way
    store._write(lambda conn: conn.execute("UPDATE conversation_turns SET user_ciphertext=?, envelope_ciphertext=NULL"
                                           " WHERE id=?", (store.cipher.encrypt(sent), tid)))
    (old,) = store.turns_by_ids([tid])
    assert old.user_text == "Restore the pages." and old.full_text == sent
    assert split_envelope("[card]\nyes do it\n\n[note]") == ("[card]\nyes do it", "\n\n[note]")
