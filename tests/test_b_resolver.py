"""Phase B, the resolver (docs/research/revamp-2026-10-02/24-build-B.md): the people and routes index (migration 28,
yuki.memory.contacts), the people directory, route ranking with a margin, the user's route preference, the referent
register and the task frame (yuki.agent.frame), the about tool, and the action weight of Yuki's own past exchanges in
related() (follow-up 4).

Temp databases and synthetic chats only; the model is a stub; nothing touches the desktop or sends anything.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yuki.memory.store as st
from yuki.memory.store import FactChange, Store

NOW = datetime(2026, 10, 3, 12, 0).timestamp()
DAY = 86400.0


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    try:
        yield s
    finally:
        s.close()


def _msg(store: Store, thread_id: int, n: int, *, sender: str | None, me: bool, at: float, text: str = "") -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label, at,"
        " text_ciphertext, first_seen, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, None, store.cipher.digest("fp", str(thread_id), str(n), text), store.cipher.digest("ck", str(n)),
         store._enc(sender), int(me), None, at, store._enc(text or f"m{n}"), at, "new")))


def _chat(store: Store, app: str, process: str, host: str, title: str, scope: str,
          lines: list[tuple[str | None, bool, float, str]]) -> int:
    tid = store.upsert_thread(app, title, f"https://{host}/{scope}", process=process, scope=scope, kind="conversation")
    for n, (sender, me, at, text) in enumerate(lines):
        _msg(store, tid, n, sender=sender, me=me, at=at, text=text)
    return tid


def world(store: Store) -> dict[str, int]:
    """Synthetic chats shaped like the real memory of 3 Oct (02 §4.1): Hemanth's Discord DM (busy, recent) and his
    WhatsApp chat (older, split across two display-name variants), a server channel he never wrote in, Elina's DM and
    the RES group she writes in, Bro's WhatsApp, person facts and a misheard name the user corrected."""
    t: dict[str, int] = {}
    dm = [("Mafex" if i % 3 else "Quasar40404", bool(i % 3), NOW - DAY * (i % 6) - i * 60,
           "bhai kab aayega" if i == 1 else f"line {i}") for i in range(60)]
    t["hemanth_dm"] = _chat(store, "Discord", "discord.exe", "discord.com", "@Quasar40404", "Quasar40404", dm)
    wa = [("You" if i % 2 else "Hemanth", bool(i % 2), NOW - DAY * 9 - i * 600, f"wa {i}") for i in range(8)]
    t["hemanth_wa"] = _chat(store, "WhatsApp.Root", "whatsapp.root", "web.whatsapp.com", "WhatsApp", "Hemanth", wa[:3])
    t["hemanth_wa2"] = store.upsert_thread("WhatsApp", "(1) WhatsApp", "https://web.whatsapp.com/Hemanth2",
                                           process="whatsapp.root", scope="Hemanth (DM)", kind="conversation")
    for n, (s, me, at, text) in enumerate(wa[3:]):
        _msg(store, t["hemanth_wa2"], 100 + n, sender=s, me=me, at=at, text=text)
    t["channel"] = _chat(store, "Discord", "discord.exe", "discord.com", "#after-hours | Mafex's Hangout",
                         "Mafex's Hangout: after-hours",
                         [("Mafex", True, NOW - DAY * 1 - i * 300, f"ch {i}") for i in range(10)]
                         + [("kestrel", False, NOW - DAY - i * 200, f"k {i}") for i in range(6)])
    t["elina_dm"] = _chat(store, "Discord", "discord.exe", "discord.com", "@pixel.fox", "pixel.fox",
                          [("Mafex", True, NOW - DAY * 2 - i * 900, f"e {i}") for i in range(6)]
                          + [("pixel.fox", False, NOW - DAY * 2 - i * 800, f"n {i}") for i in range(6)])
    t["res"] = _chat(store, "WhatsApp", "whatsapp.root", "web.whatsapp.com", "(1) WhatsApp", "RES",
                     [("You", True, NOW - DAY * 2 - i * 700, f"r {i}") for i in range(20)]
                     + [("Elina BAC", False, NOW - DAY * 2 - i * 650, f"eb {i}") for i in range(8)]
                     + [("rohan BAC", False, NOW - DAY * 2 - i * 640, f"rb {i}") for i in range(16)])
    t["bro"] = _chat(store, "WhatsApp", "whatsapp.root", "web.whatsapp.com", "WhatsApp", "Bro",
                     [("You", True, NOW - DAY * 3 - i * 500, "ok laminate kar dunga" if i == 0 else f"b {i}")
                      for i in range(10)] + [("Bro", False, NOW - DAY * 3 - i * 450, f"bb {i}") for i in range(6)])
    t["shop"] = _chat(store, "Google Chrome", "chrome.exe", "www.amazon.in", "Amazon.in : headphones", "Amazon.in",
                      [("Amazon", False, NOW - DAY, "deal")])
    facts = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "person", "Hemanth", "Hemanth is the user's best friend, whom they talk to on WhatsApp "
                   "and Discord (handle 'quasar'/'Quasar40404').", 0.9, [], NOW - 5 * DAY),
        FactChange("ADD", None, "person", "Elina", "Elina (Discord handle pixel.fox) is someone the user talks to in a "
                   "Discord DM and the WhatsApp group RES.", 0.8, [], NOW - 5 * DAY),
        FactChange("ADD", None, "person", "Bro", "Bro is the user's brother, whom they talk to on WhatsApp about football "
                   "plans.", 0.9, [], NOW - 5 * DAY)])
    t["f_hemanth"], t["f_elina"], t["f_bro"] = [f for _ok, f in facts]
    store.add_alias("Himanth", "Hemanth", why="speech recognition misheard his name")
    return t


def gold_answer(t: dict[str, int]) -> dict[str, Any]:
    """What a good linking call returns for :func:`world`."""
    return {
        "chats": [
            {"threads": [t["hemanth_dm"]], "kind": "dm", "name": "@Quasar40404"},
            {"threads": [t["hemanth_wa"], t["hemanth_wa2"]], "kind": "dm", "name": "Hemanth"},
            {"threads": [t["channel"]], "kind": "channel", "name": "Mafex's Hangout: after-hours"},
            {"threads": [t["elina_dm"]], "kind": "dm", "name": "pixel.fox"},
            {"threads": [t["res"]], "kind": "group", "name": "RES"},
            {"threads": [t["bro"]], "kind": "dm", "name": "Bro"},
            {"threads": [t["shop"]], "kind": "not_a_chat", "name": "Amazon.in"},
        ],
        "people": [
            {"id": None, "name": "Hemanth", "relation": "best friend", "facts": [t["f_hemanth"]],
             "aliases": ["Quasar40404", "quasar"], "tone": "Hinglish, lowercase, casual",
             "routes": [{"threads": [t["hemanth_dm"]], "handle": "Quasar40404", "journal": []},
                        {"threads": [t["hemanth_wa"], t["hemanth_wa2"]], "handle": "Hemanth", "journal": []}]},
            {"id": None, "name": "Elina", "relation": "", "facts": [t["f_elina"]], "aliases": ["pixel.fox", "Elina BAC"],
             "tone": "casual", "routes": [{"threads": [t["elina_dm"]], "handle": "pixel.fox", "journal": []},
                                          {"threads": [t["res"]], "handle": "Elina BAC", "journal": []}]},
            {"id": None, "name": "Bro", "relation": "brother", "facts": [t["f_bro"]], "aliases": [],
             "tone": "Hindi and English", "routes": [{"threads": [t["bro"]], "handle": "Bro", "journal": []}]},
        ],
        "retired": [],
        "questions": [],
    }


class _Worker:
    """A stand-in for the portrait worker: its model call returns a fixed answer."""

    model = "stub-sonnet"
    effort = "low"

    def __init__(self, store: Store, answer: dict[str, Any]) -> None:
        self.store = store
        self.answer = answer
        self.settings = SimpleNamespace(user_names=["Kian", "Mafex"])
        self.embedder = None
        self.calls: list[dict[str, Any]] = []
        self.client = None

    def _call(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        kw["stats"].cost_usd += 0.01
        return self.answer

    def _log(self, *a: Any, **k: Any) -> None:
        pass

    def _seal(self, v: Any) -> str:
        return self.store.cipher.encrypt(json.dumps(v, default=str))

    def vet_names(self) -> list[str]:
        return ["Kian", "Mafex"]


def built(store: Store) -> dict[str, int]:
    """:func:`world` linked by the stub model (the index the other tests read)."""
    from yuki.memory.contacts import Linker

    t = world(store)
    res = Linker(_Worker(store, gold_answer(t))).run(kind="rebuild", now=NOW)
    assert res.ok and not res.rejected, res.rejected
    return t


# -- migration 28 -----------------------------------------------------------------------------------------------


def test_migration_28_adds_the_people_tables_and_keeps_everything(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    full = st.MIGRATIONS
    monkeypatch.setattr(st, "MIGRATIONS", full[:27])
    old = Store.open(path)
    tid = old.upsert_thread("Discord", "@Quasar40404", None, process="discord.exe", scope="Quasar40404",
                            kind="conversation")
    _msg(old, tid, 1, sender="Quasar40404", me=False, at=NOW)
    assert old._query("PRAGMA user_version")[0][0] == 27
    assert not old._query("SELECT name FROM sqlite_master WHERE name='contacts'")
    old.close()
    monkeypatch.setattr(st, "MIGRATIONS", full)
    new = Store.open(path)
    try:
        assert new._query("PRAGMA user_version")[0][0] == len(st.MIGRATIONS) >= 28
        for table in ("contacts", "contact_routes", "contact_prefs"):
            assert new._query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        assert new._query("SELECT count(*) FROM messages")[0][0] == 1                      # nothing moved or lost
        assert new.thread_info(tid).title == "@Quasar40404"
        from yuki.memory import contacts

        assert contacts.people(new) == [] and contacts.directory(new) == ""                # empty until linked
    finally:
        new.close()


# -- linking: the model's answer checked against the stored strings -------------------------------------------------


def test_linking_keeps_only_what_the_stored_rows_say(store):
    from yuki.memory.contacts import gather, validate

    t = world(store)
    cands = gather(store, now=NOW, settings_names=["Kian", "Mafex"])
    assert t["hemanth_dm"] in cands.threads and cands.threads[t["hemanth_dm"]].senders == {"Quasar40404": 20}
    assert cands.threads[t["hemanth_dm"]].own_lines   # the user's own last lines, for the tone
    answer = gold_answer(t)
    answer["chats"].append({"threads": [t["bro"]], "kind": "dm", "name": "Bro again"})        # a thread used twice
    answer["chats"][1] = dict(answer["chats"][1], name="Hemanth (WhatsApp)")                     # not a stored name
    answer["people"][0]["aliases"] += ["Harry"]                                                  # invented alias
    answer["people"][0]["relation"] = "cousin"                                                   # not the fact's words
    answer["people"][0]["routes"].append({"threads": [t["elina_dm"]], "handle": "pixel.fox", "journal": []})  # her DM
    answer["people"][0]["routes"].append({"threads": [t["channel"]], "handle": "", "journal": []})  # never wrote there
    answer["people"].append({"id": None, "name": "Mafex", "relation": "", "facts": [], "aliases": [], "tone": "",
                             "routes": [{"threads": [t["channel"]], "handle": "Mafex", "journal": []}]})  # the user
    answer["people"].append({"id": None, "name": "Kunal", "relation": "", "facts": [], "aliases": [], "tone": "",
                             "routes": []})                                                         # in no row
    answer["questions"] = [{"subject": "rohan BAC / Ravi2099", "question": "Is 'rohan BAC' the same person as "
                            "Ravi2099?", "evidence": "RES on WhatsApp", "why": "two names", "importance": 5,
                            "threads": [t["res"]], "facts": []},
                           {"subject": "x", "question": "y", "evidence": "", "why": "", "importance": 3,
                            "threads": [], "facts": []}]
    out = validate(answer, cands)
    names = {p["name"]: p for p in out.people}
    assert set(names) == {"Hemanth", "Elina", "Bro"}
    h = names["Hemanth"]
    assert h["relation"] is None                                         # "cousin" is in no cited fact: dropped
    assert [a["name"] for a in h["aliases"]] == ["Quasar40404", "quasar", "Himanth"]   # + the misheard form, as heard
    assert h["aliases"][-1]["how"] == "heard"
    assert [r["chat"]["name"] for r in h["routes"]] == ["@Quasar40404"]  # the WhatsApp chat's name was refused
    rejected = " | ".join(out.rejected)
    for why in ("'Harry' is in none of the stored rows", "relation 'cousin'", "a DM that is not theirs",
                "a group where they never wrote", "that is the user", "'Kunal': the name is in none",
                "not one of its titles", "a question without"):
        assert why in rejected, why
    assert len(out.questions) == 1 and out.questions[0]["threads"] == [t["res"]]
    assert names["Bro"]["relation"] == "brother"


def test_a_mis_serialised_answer_is_read_back(store):
    """3 Oct, the real memory: Sonnet (no strict schema on Bedrock) sent the whole answer as a string in "chats"."""
    from yuki.memory.contacts import gather, repair, validate

    t = world(store)
    answer = gold_answer(t)
    whole = {"chats": json.dumps(answer), "people": "", "retired": [], "questions": []}
    assert repair(whole)["people"] == answer["people"]
    tail = {"chats": json.dumps(answer["chats"])[1:-1] + '], "people": ' + json.dumps(answer["people"])[:-1],
            "people": None}
    assert len(repair({"chats": json.dumps(answer["chats"]), "people": answer["people"]})["chats"]) == 7
    assert isinstance(repair(tail), dict)                                  # never raises on a broken string
    out = validate(whole, gather(store, now=NOW, settings_names=["Mafex"]))
    assert {p["name"] for p in out.people} == {"Hemanth", "Elina", "Bro"}


def test_a_group_route_needs_them_writing_there_or_a_cited_entry_naming_them(store):
    from yuki.memory.contacts import gather, group_journal, validate

    t = world(store)
    j = store.add_journal(NOW - DAY, t["channel"], "Discord", "The user tagged Hemanth in #after-hours asking when "
                          "he will join the voice chat.", 5)
    store.add_journal(NOW - DAY, t["hemanth_dm"], "Discord", "Hemanth replied on Discord.", 4)
    cands = gather(store, now=NOW, settings_names=["Mafex"])
    cands.journal = group_journal(store, cands)
    assert j in cands.journal                         # an entry of a group thread naming a known person is offered
    answer = gold_answer(t)
    answer["people"][0]["routes"].append({"threads": [t["channel"]], "handle": "", "journal": [j]})
    out = validate(answer, cands)
    h = next(p for p in out.people if p["name"] == "Hemanth")
    assert [r["chat"]["name"] for r in h["routes"]][-1] == "Mafex's Hangout: after-hours"
    assert h["routes"][-1]["evidence"] == [j]


def test_a_rebuild_writes_people_routes_and_places_and_keeps_history(store):
    from yuki.memory import contacts

    t = built(store)
    found = {c.name: c for c in contacts.people(store)}
    assert set(found) == {"Hemanth", "Elina", "Bro"}
    wa = next(r for r in found["Hemanth"].routes if r.service == "web.whatsapp.com")
    assert sorted(wa.thread_ids) == sorted([t["hemanth_wa"], t["hemanth_wa2"]])   # both display-name variants
    assert {r.chat for r in contacts.places(store)} == {"Mafex's Hangout: after-hours", "RES"}
    # a second rebuild retires the first rows (history kept) and writes fresh ones
    from yuki.memory.contacts import Linker

    res = Linker(_Worker(store, gold_answer(t))).run(kind="rebuild", now=NOW + 60)
    assert res.ok and res.counts["retired"] == 3
    assert store._query("SELECT count(*) FROM contacts")[0][0] == 6
    assert store._query("SELECT count(*) FROM contacts WHERE ended_at IS NULL")[0][0] == 3
    # a refresh with nothing changed since makes no model call
    worker = _Worker(store, gold_answer(t))
    assert Linker(worker).run(kind="refresh").skipped and not worker.calls


def test_the_nightly_pass_refreshes_the_index_only_when_memory_changed(store):
    import threading

    from yuki.memory.portrait import PortraitScheduler

    t = world(store)
    worker = _Worker(store, gold_answer(t))
    worker._run_lock = threading.Lock()
    logged: list[dict[str, Any]] = []
    sched = PortraitScheduler(store, worker, db_path=store.path, log=lambda type, **f: logged.append({"type": type, **f}))
    sched._contacts("nightly")                                 # no run yet: the first refresh links everyone
    assert len(worker.calls) == 1 and logged[-1]["kind"] == "contacts" and logged[-1]["people"] == 3
    # (the synthetic chats are dated later today: the run is put after them)
    store._write(lambda conn: conn.execute("UPDATE portrait_runs SET at=? WHERE kind='contacts'", (NOW + 10,)))
    sched._contacts("nightly")                                 # nothing changed since: no model call
    assert len(worker.calls) == 1 and logged[-1].get("skipped")
    _msg(store, t["bro"], 999, sender="Bro", me=False, at=NOW + 60)          # a new message arrives
    sched._contacts("nightly")
    assert len(worker.calls) == 2 and logged[-1].get("counts", {}).get("updated") == 3


def test_ambiguous_merges_become_questions_through_the_pipeline(store, monkeypatch):
    from yuki.memory import contacts, questions

    t = world(store)
    answer = gold_answer(t)
    answer["questions"] = [{"subject": "rohan BAC", "question": "Is 'rohan BAC' on WhatsApp the same person as "
                            "Ravi2099 on Discord?", "evidence": "the RES group", "why": "two names, one person?",
                            "importance": 5, "threads": [t["res"]], "facts": []}]
    vetted: list[Any] = []
    monkeypatch.setattr(questions, "vet_new", lambda store, qs, **kw: vetted.extend(qs) or list(qs))
    res = contacts.Linker(_Worker(store, answer)).run(kind="rebuild", now=NOW)
    assert res.asked == 1 and len(vetted) == 1
    [q] = store.questions(("open",))
    assert q.kind == "person" and q.origin == "contacts" and "rohan BAC" in q.question


# -- ranking, margin, preference, directory ------------------------------------------------------------------------


def test_routes_rank_by_the_users_own_recent_messages_with_a_margin(store):
    from yuki.memory import contacts

    built(store)
    found = {c.name: c for c in contacts.people(store)}
    h = contacts.rank_routes(store, found["Hemanth"], now=NOW)
    assert h.usual.chat == "@Quasar40404" and not h.close and h.margin > 0.9
    assert h.routes[0].mine == 40 and h.routes[0].theirs == 20 and h.routes[1].mine == 4
    e = contacts.rank_routes(store, found["Elina"], now=NOW)
    # her DM (6 of the user's messages) and the RES group (20, scaled by her share of the others there, 8 of 24)
    assert {r.chat for r in e.routes[:2]} == {"pixel.fox", "RES"} and e.close and e.margin < contacts.ROUTE_MARGIN
    res = next(r for r in e.routes if r.chat == "RES")
    assert res.theirs == 8 and abs(res.share - 8 / 24) < 1e-6
    assert contacts.rank_routes(store, found["Bro"], now=NOW).margin == 1.0     # one route: no doubt


def test_the_users_route_preference_comes_first_and_survives_a_rebuild(store):
    from yuki.memory import contacts

    t = built(store)
    h = next(c for c in contacts.people(store) if c.name == "Hemanth")
    wa = next(r for r in h.routes if r.service == "web.whatsapp.com")
    assert not contacts.prefer_route(store, h, wa, "")["ok"]                   # the user's words are required
    other = next(c for c in contacts.people(store) if c.name == "Bro")
    assert not contacts.prefer_route(store, other, wa, "use whatsapp")["ok"]  # not Bro's route
    assert contacts.prefer_route(store, h, wa, "no, the other one - whatsapp for him")["ok"]
    ranked = contacts.rank_routes(store, next(c for c in contacts.people(store) if c.name == "Hemanth"), now=NOW)
    assert ranked.usual.service == "web.whatsapp.com" and ranked.usual.preferred and ranked.margin == 1.0
    assert ranked.preferred["quote"] == "no, the other one - whatsapp for him"
    contacts.Linker(_Worker(store, gold_answer(t))).run(kind="rebuild", now=NOW + 60)
    again = contacts.rank_routes(store, next(c for c in contacts.people(store) if c.name == "Hemanth"), now=NOW)
    assert again.usual.service == "web.whatsapp.com"                          # keyed by name and chat, not by ids


def test_the_directory_is_compact_ranked_and_stable(store):
    from yuki.memory import contacts

    built(store)
    text = contacts.directory(store, now=NOW)
    assert len(text) <= contacts.DIRECTORY_CHARS
    lines = text.splitlines()
    assert lines[0] == contacts.DIRECTORY_HEAD and lines[1].startswith("- Hemanth (best friend")
    assert "heard as Himanth" in lines[1] and "Discord DM @Quasar40404 > WhatsApp DM Hemanth" in lines[1]
    assert "Hinglish" in lines[1]
    assert any(line.startswith(contacts.PLACES_HEAD) and "after-hours" in line for line in lines)
    assert not any(ch.isdigit() for ch in text.replace("Quasar40404", ""))  # no counts or dates in a cached layer
    assert contacts.directory(store, now=NOW + 3600) == text


def test_names_are_found_as_whole_words_heard_forms_included(store):
    from yuki.memory import contacts

    built(store)
    found = [(c.name, w) for c, w in contacts.named_in(store, "tell himanth i'm coming, and ask Bro too")]
    assert found == [("Hemanth", "Himanth"), ("Bro", "Bro")]
    assert contacts.named_in(store, "my brother said broke") == []           # whole words only
    assert [c.name for c in contacts.resolve(store, "quasar40404")] == ["Hemanth"]
    assert [c.name for c in contacts.resolve(store, "Elina BAC")] == ["Elina"]
    h = contacts.resolve(store, "Hemanth")[0]
    assert contacts.resolve(store, h.routes[0].ref)[0].name == "Hemanth"


def test_the_card_has_counts_margin_and_the_users_own_last_messages(store):
    from yuki.memory import contacts

    t = built(store)
    h = contacts.resolve(store, "Hemanth")[0]
    c = contacts.card(store, h, now=NOW, near_thread=t["hemanth_dm"])
    assert c["routes"][0]["in_front"] and c["routes"][0]["mine"] == 40 and len(c["routes"][0]["samples"]) == 5
    text = contacts.card_text(c)
    assert "Hemanth - best friend" in text and "the user 40 / them 20" in text and "Usual: the first, clearly" in text
    assert "heard the name as: Himanth" in text and "in front now" in text and "\"line " in text
    e = contacts.card_text(contacts.card(store, contacts.resolve(store, "Elina")[0], now=NOW))
    assert "Close call" in e


# -- the referent register and the task frame (yuki.agent.frame) ----------------------------------------------------


def _person(pid: int, name: str, **kw: Any) -> dict[str, Any]:
    return {"id": pid, "ref": f"contact:{pid}", "name": name, "relation": None, "heard": [], **kw}


def test_the_referent_register_lists_candidates_with_source_age_and_a_margin():
    from yuki.agent import frame

    facts = {"front": {"kind": "dm", "chat": "Quasar40404", "people": [_person(1, "Hemanth", route="Discord DM")]},
             "said": [_person(2, "Elina", words="Elina", at=NOW - 60, who="yuki"),
                      _person(3, "Bro", words="bro", at=NOW - 1800, who="user")],     # half an hour: faded out
             "recent": [_person(4, "Ezra", route="Discord DM \"Ezra\"", at=NOW - 240)],
             "call": {"app": "Discord", "people": [_person(5, "kestrel", at=NOW - 30, how="wrote in the call app")]}}
    people, things = frame.referents(facts, now=NOW)
    assert [c.label for c in people] == ["Hemanth", "Elina", "kestrel", "Ezra"]
    assert people[0].source == "front" and people[0].strength == 1.0
    assert people[1].source == "said_yuki" and 0.9 < people[1].strength < 1.0 and people[1].age_s == 60
    assert frame.close_call(people)                                   # Hemanth in front vs Elina named a minute ago
    assert not frame.close_call(people[:1]) and things == []
    # the user's own words weigh a little less than Yuki naming someone (a name can be a word in their mouth)
    assert frame.strength("said_user", 60) < frame.strength("said_yuki", 60)
    # a group in front: its people, the latest to write first, as candidates (not "the" person)
    group = {"front": {"kind": "group", "chat": "RES", "people": [_person(6, "Rohan", last_theirs=NOW - 50),
                                                                  _person(2, "Elina", last_theirs=NOW - 100)]}}
    people, _ = frame.referents(group, now=NOW)
    assert [c.label for c in people] == ["Rohan", "Elina"] and all(c.source == "recent" for c in people)


def test_this_is_the_selection_or_the_clipboard_metadata_never_private_text():
    from yuki.agent import frame

    frame._CLIP_STATE.update(seq=None, first_seen=None, looked=None)

    class Clip:
        def __init__(self, seq: int, text: str | None, private: bool = False) -> None:
            self.ok, self.sequence, self.text, self.private = True, seq, text, private
            self.chars = len(text or "x" * 40)
            self.formats = ["Unicode text"]
            self.owner_process = "chrome.exe"

    box = {"clip": Clip(7, "https://example.com/a-very-long-link " + "x" * 300)}
    backend = SimpleNamespace(read_clipboard=lambda: box["clip"])
    first = frame.clipboard_meta(backend, now=NOW)
    assert first["age_s"] is None and first["since"] is None and len(first["preview"]) <= frame.CLIP_PREVIEW
    again = frame.clipboard_meta(backend, now=NOW + 40)
    assert again["age_s"] == 40                                        # the same copy, seen 40 s ago
    box["clip"] = Clip(8, None, private=True)
    secret = frame.clipboard_meta(backend, now=NOW + 100)
    assert secret["private"] and secret["preview"] is None and secret["changed"] and secret["since"] == NOW + 40
    _people, things = frame.referents({}, clip=secret, now=NOW + 100)
    assert things and "marked private (not read)" in things[0].label
    focus = SimpleNamespace(ok=True, role="Edit", selection="the deck link", window=9, window_title="Slack",
                            line=lambda limit=160: "focus: Edit")
    _people, things = frame.referents({}, focus=focus, clip=again, now=NOW + 40)
    assert things[0].source == "selection" and "the deck link" in things[0].label
    assert things[1].source == "clipboard" and "copied 40 s ago or earlier from chrome.exe" in things[1].where
    stale = dict(again, age_s=frame.CLIP_FRESH_S + 1)
    assert frame.referents({}, clip=stale, now=NOW)[1] == []           # an old copy is not a likely "this"
    assert frame.clipboard_meta(SimpleNamespace(), now=NOW) is None     # a desk that cannot read it
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"                 # a key copied as plain text
    box["clip"] = Clip(9, f"my token {token}")
    shown = frame.clipboard_meta(backend, now=NOW + 200)
    assert token not in (shown["preview"] or "") and shown["preview"].startswith("my token")


def test_the_frame_is_empty_when_nothing_clears_a_floor_and_bounded_otherwise():
    from yuki.agent import frame

    empty = frame.build({}, now=NOW)
    assert empty.text == "" and not empty.action and empty.error is None
    faded = frame.build({"said": [_person(2, "Elina", words="Elina", at=NOW - 3 * 3600, who="yuki")]}, now=NOW)
    assert faded.text == ""                                            # three hours ago: below the floor
    many = {"named": [_person(1, "Hemanth", words="Hemanth")],
            "routes": {1: {"name": "Hemanth", "margin": 0.9, "close": False, "preferred": None,
                           "routes": [{"ref": f"route:{i}", "label": f"Discord DM \"{'x' * 300}{i}\"", "mine": 9,
                                       "theirs": 3, "kind": "dm", "processes": ["discord"]} for i in range(4)]}},
            "pitfalls": [{"app": "discord", "text": "y" * 400}] * 3}
    big = frame.build(many, knowhow=["[discord] " + "z" * 600] * 3, now=NOW)
    assert big.text.startswith(frame.LABEL) and big.text.endswith(frame.END)
    assert len(big.text) <= frame.FRAME_CHARS and big.action
    body = big.text[: -len(frame.END)].rstrip()
    assert not body.endswith(":")                                      # no heading left without its items


def test_the_frame_resolves_him_to_the_dm_in_front_with_routes_preconditions_and_stakes(store):
    from yuki.agent import frame
    from yuki.memory.api import MemoryClient

    built(store)
    client = MemoryClient(store)
    screen = {"app": "Discord", "process": "discord.exe", "title": "@Quasar40404",
              "url": "https://discord.com/Quasar40404", "scope": "Quasar40404"}
    client.frame_facts("warm the cache", screen=screen, now=NOW)
    started = time.perf_counter()
    facts = client.frame_facts("message him this", said=[{"at": NOW - 900, "text": "Bro is your brother, yes.",
                                                          "who": "yuki"}], screen=screen, now=NOW)
    overview = {"windows": [{"hwnd": 0x2002, "process_name": "Discord.exe", "title": "@Quasar40404 - Discord",
                             "is_foreground": True}]}
    f = frame.build(facts, overview=overview, now=NOW)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert facts["front"]["kind"] == "dm" and facts["front"]["people"][0]["name"] == "Hemanth"
    assert [p.label for p in f.people][:2] == ["Hemanth", "Bro"] and not f.close_people
    assert f.persons[0]["name"] == "Hemanth" and f.action
    text = f.text
    assert "Hemanth (best friend) - the strongest referent" in text
    assert "1. [route:" in text and "Discord DM \"@Quasar40404\"" in text and "in front now" in text
    assert "Usual: the first, clearly" in text and "Discord is open: window [8194]" in text
    assert "reaches Hemanth; once sent it cannot be taken back" in text
    assert elapsed_ms < 50, elapsed_ms                                  # the frame is cheap (SQL and code only)
    # a request that names someone gets that person, whatever is in front
    named = frame.build(client.frame_facts("tell Himanth I'm coming", screen=screen, now=NOW), now=NOW)
    assert named.persons[0]["name"] == "Hemanth" and "named in the request (\"Himanth\")" in named.text
    elina = frame.build(client.frame_facts("reply to Elina in the group", now=NOW), now=NOW)
    assert "Close call between the first two" in elina.text            # her DM and RES are about even
    # "my usual channel": the group chats on the same apps as the person's routes come with them
    tag = frame.build(client.frame_facts("tag Hemanth in my usual channel", now=NOW), now=NOW)
    assert "Group chats and channels on the same apps" in tag.text and "after-hours" in tag.text
    assert "never the DM" in tag.text                                  # revamp D personal: the channel example
    # a person only remembered from the conversation keeps one line with the usual route, no block, no action
    later = frame.build(client.frame_facts("lower your volume", said=[{"at": NOW - 200, "text": "Hemanth is your best "
                                                                       "friend.", "who": "yuki"}], now=NOW), now=NOW)
    assert later.persons == [] and not later.action
    assert "- Hemanth: named in Yuki's reply" in later.text and "usually [route:" in later.text
    assert "Stakes" not in later.text and len(later.text) < 600
    # Yuki named someone two minutes ago while another's DM is in front: the frame calls it close
    close = frame.build(client.frame_facts("message him this", said=[{"at": NOW - 120, "text": "Bro is your brother.",
                                                                      "who": "yuki"}], screen=screen, now=NOW), now=NOW)
    assert close.close_people and "close call" in close.text


def test_the_about_tool_shows_the_card_and_keeps_a_route_choice_only_in_the_users_words(store):
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    built(store)
    memory = MemoryAccess(open_client=lambda: MemoryClient(store))
    memory.run_tool("about", {"name": "Bro"}, request="x")              # opens the client
    started = time.perf_counter()
    out = memory.run_tool("about", {"name": "himanth"}, request="message himanth")
    assert (time.perf_counter() - started) * 1000 < 100                 # no model call
    text = out.content[0]["text"]
    assert out.ok and "Hemanth - best friend" in text and "[route:" in text and "last messages" in text
    routes = out.payload["routes"]
    wa = routes[1]
    refused = memory.run_tool("about", {"name": "Hemanth", "prefer_route": wa, "words": "always whatsapp"},
                              request="no, the other one")
    assert not refused.ok and "user's own words" in refused.content[0]["text"]
    kept = memory.run_tool("about", {"name": "Hemanth", "prefer_route": wa, "words": "the other one"},
                           request="no, the other one")
    assert kept.ok and kept.payload["saved"]
    again = memory.run_tool("about", {"name": "Hemanth"}, request="x")
    assert again.payload["routes"][0] == wa and "the user chose this one" in again.content[0]["text"]
    unknown = memory.run_tool("about", {"name": "Zed"}, request="x")
    assert unknown.ok and not unknown.payload["found"] and "Hemanth" in unknown.content[0]["text"]
    assert memory.run_tool("about", {}, request="x").ok is False        # a name is required


# -- follow-up 4: Yuki's own past exchanges weigh less for an action request --------------------------------------


def test_an_action_request_weighs_yukis_own_past_exchanges_from_other_days_less(store):
    import numpy as np

    from yuki.memory.api import MemoryClient
    from yuki.memory.related import ACTION_OWN_DISCOUNT, ACTION_OWN_PER_KIND

    dim = 8

    def vec(*parts: tuple[int, float]) -> np.ndarray:
        v = np.zeros(dim, dtype=np.float32)
        for i, w in parts:
            v[i] = w
        return v / np.linalg.norm(v)

    class Embedder:
        model_name = "stub"

        def embed(self, texts, wait_s=None):
            return np.stack([vec((1, 1.0)) for _ in texts])

    client = MemoryClient(store)
    client._embedder = Embedder()
    now = time.time()
    for k in range(3):   # Yuki's own exchanges of other days, very near the words
        tid = store.add_turn("old", 1, now - (k + 2) * 86400, f"restore the pages {k}", "Done.", ["click"], "final")
        store.flush_turns()
        store.add_conversation_vectors("turn", [(tid, vec((1, 1.0), (2 + k, 0.5)))], "stub")   # cosine 0.89
    thread = store.upsert_thread("Google Chrome", "Restore pages", "https://x.test/", process="chrome.exe")
    for k in range(2):   # facts a little less near (cosine 0.85)
        store.add_journal(now - 3600 * (k + 1), thread, "Google Chrome", f"The user restored Chrome pages {k}.", 5,
                          vector=vec((1, 0.85), (5 + k, 0.53)))
    plain = client.related("restore the pages", now=now, with_recent=False, embed_wait_s=1.0, diet=True)
    acting = client.related("restore the pages", now=now, with_recent=False, embed_wait_s=1.0, diet=True, action=True)
    own = [i for i in plain["items"] if i["kind"] == "chat"]
    own_acting = [i for i in acting["items"] if i["kind"] == "chat"]
    assert len(own) == 3 and len(own_acting) == ACTION_OWN_PER_KIND
    assert acting["own_discounted"] == 3 and plain["own_discounted"] == 0 and acting["action"]
    assert ACTION_OWN_DISCOUNT > 0
    # an exchange from earlier today keeps its weight (the day's own context)
    today = store.add_turn("today", 1, now - 3 * 3600, "restore the pages again", "Done.", ["click"], "final")
    store.flush_turns()
    store.add_conversation_vectors("turn", [(today, vec((1, 1.0)))], "stub")
    again = client.related("restore the pages", now=now, with_recent=False, embed_wait_s=1.0, diet=True, action=True)
    assert [i["id"] for i in again["items"] if i["kind"] == "chat"] == [today]


# -- the agent: the directory in the cached profile layer, the frame before the words --------------------------------


def test_the_agent_puts_the_directory_in_the_profile_and_the_frame_before_the_words(tmp_path):
    from yuki.agent.frame import LABEL
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess, MemoryContext
    from yuki.config import Settings
    from yuki.log.events import SessionLogger
    from yuki.voice.offline import StubDesktop

    directory = "People the user talks to (...):\n- Hemanth (best friend): Discord DM Quasar40404 > WhatsApp DM Hemanth"

    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: SimpleNamespace())
            self.asked: list[dict[str, Any]] = []
            self.framed: list[dict[str, Any]] = []

        def context(self, **kw: Any) -> MemoryContext:  # type: ignore[override]
            self.asked.append(kw)
            return MemoryContext(source="memory", people=directory)

        def frame_facts(self, words: str, **kw: Any) -> dict[str, Any]:  # type: ignore[override]
            self.framed.append({"words": words, **kw})
            return {"front": {"kind": "dm", "chat": "Quasar40404",
                              "people": [_person(1, "Hemanth", route="Discord DM \"Quasar40404\"")]},
                    "routes": {1: {"name": "Hemanth", "relation": "best friend", "margin": 0.9, "close": False,
                                   "preferred": None, "routes": [{"ref": "route:2", "label": "Discord DM \"Quasar40404\"",
                                                                  "mine": 78, "theirs": 47, "kind": "dm",
                                                                  "in_front": True, "processes": ["discord"]}]}}}

    sent: list[dict[str, Any]] = []
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    done = SimpleNamespace(type="tool_use", id="d", name="done", input={"message": "Sent to Hemanth on Discord.",
                                                                       "about": []})
    reply = SimpleNamespace(content=[done], stop_reason="tool_use", usage=usage, id="m", model="m", role="assistant",
                            model_dump=lambda **_: {})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: sent.append(p) or reply))
    memory = Memory()
    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)
    list(agent.run("message him this"))
    profile = " ".join(b["text"] for b in sent[0]["system"])
    assert "- Hemanth (best friend): Discord DM Quasar40404" in profile
    first = json.dumps(sent[0]["messages"][0]["content"], ensure_ascii=False)
    assert LABEL[:40] in first and "Hemanth (best friend) - the strongest referent" in first
    assert first.index(LABEL[:40]) < first.index("message him this")
    assert memory.asked[0]["action"] is True                           # a person in play: an action request
    assert agent.last_summary["memory"]["frame"]["persons"] == 1
    # the next request sees this exchange as "said": Yuki's reply named Hemanth
    list(agent.run("and send it on whatsapp too"))
    said = memory.framed[1]["said"]
    assert said[-1]["who"] == "yuki" and "Hemanth" in said[-1]["text"]
    assert said[-2]["who"] == "user" and said[-2]["text"] == "message him this"
