"""The window's pages for the 3 Oct feedback (report 45, Builder B of report 38): what the page data says and what the
window's actions do, over stand-in memories with invented data. Formats only at render (natural dates, 12-hour
times), memory's shown forms everywhere, one summary per person, each fact once, the review queue and misheard
answered at once, the ledger grouped by request with no Undo, Day sessions that open to every app, a call that names
a friend once, the thread's days. The real memory, the tray and the desktop are never touched."""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from yuki.ui import web_data as W
from yuki.ui import web_window
from yuki.ui.window import data as D

NOW = datetime(2026, 10, 3, 21, 52).timestamp()
H, DAY = 3600.0, 86400.0


# -- render-time formats ---------------------------------------------------------------------------------------------


def test_only_the_date_and_time_formats_change_and_the_old_word_pass_is_gone():
    today = date(2026, 10, 3)
    assert D.natural("On 2026-09-25 from about 02:00 to 03:22.", today=today) == "On 25 September from about 2 AM to 3:22 AM."
    assert D.natural("At 2026-10-02T21:06:00 and 21:52", today=today) == "At 2 October, 9:06 PM and 9:52 PM"
    assert D.natural("already 9:30 PM, took 1:09:30", today=today) == "already 9:30 PM, took 1:09:30"   # never twice
    assert D.natural("on 2025-12-31", today=today) == "on 31 December 2025"                         # another year
    assert D.natural("The user said they would", today=today) == "The user said they would"         # no word matched
    assert not hasattr(D, "second_person")
    assert D.nat_when(datetime(2026, 10, 2, 2, 29).timestamp(), today=today) == "yesterday, 2:29 AM"
    assert W.day_words(datetime(2026, 10, 1, 23, 40).timestamp(), today=today) == "1 Oct, 11:40 PM"


# -- Me ----------------------------------------------------------------------------------------------------------------


def _ev(at, app, call=None):
    return {"at": at, "app": app, "text": "", "nature": "fact", "call": f"call:{call}" if call else None}


def _fact(fid, kind, subject, summary, shown, *, ev, at=NOW - 2 * H, origin="model", confidence=0.7, pinned=False,
          aspect=None, text=None):
    return {"id": fid, "ref": f"fact:{fid}", "kind": kind, "subject": subject, "summary": summary, "shown": shown,
            "text": text or "The user did something on 2026-10-01 at 14:05.", "aspect": aspect, "origin": origin,
            "confidence": confidence, "since": at, "created_at": at, "pinned": pinned, "importance": 4, "evidence": ev}


class _MeMemory:
    def people_list(self, limit=40):
        return [{"ref": "person:8", "name": "Noor", "relation": "Bandmate", "aliases": ["noor.plays"], "usual": "route:24",
                 "last_contact": NOW - DAY, "routes": [{"app": "Signal", "kind": "dm", "chat": "Noor", "handle": "noor",
                                                         "mine": 9, "theirs": 10, "last_mine": NOW - DAY, "preferred": True}],
                 "summary": {"ref": "fact:24", "headline": "Noor runs the seed swap", "text": "She runs the swap on the 18th.",
                             "pinned": False}},
                {"ref": "person:5", "name": "Dev", "relation": "Your brother", "aliases": [], "usual": "Email, short.",
                 "last_contact": NOW - H, "routes": [], "summary": None}]

    def portrait_facts_with_evidence(self, evidence_per_fact=6):
        days = [_ev(NOW - d * DAY - H, "VS Code") for d in (0, 1, 3)]
        return [
            _fact(12, "work", "Planner", "Garden planner, in TypeScript", "You’re building a garden planner.", ev=days, pinned=True),
            _fact(13, "open_loop", "Swap", "You’ll confirm the tables", None, ev=[_ev(NOW - DAY, "call:mumble", 7)],
                  text="The user said on 2026-10-02 at 19:15 they would confirm the tables."),
            _fact(24, "person", "Noor", "Noor runs the seed swap", "She runs the swap on the 18th.", ev=days),
            _fact(31, "routine", "Evenings", "Evenings at the desk", "Most evenings you sit down at 8:30 PM.", ev=days, confidence=0.86),
            _fact(34, "behaviour", "Packets", "Sorted seed packets", "On 1 October you sorted seed packets.", ev=days),
            _fact(40, "work", "Moving", "You might move in spring", "You said you might move in spring.", ev=days),
            _fact(16, "personality", "Basil", "Your basil has a personality", "A joke.", ev=days, aspect="joke"),
        ]

    def review_queue(self, now=None):
        return {"items": [{"ref": "fact:40", "headline": "You might move in spring", "text": "You said you might move in spring.",
                           "flag": "new"}], "cap": 5, "reviewed_today": 0, "left_today": 5, "waiting": 3,
                "merged": {"count": 2, "since": NOW - 7 * DAY}}

    def misheard(self, days=7, limit=8, now=None):
        return {"items": [{"id": 812, "ref": "journal:812", "at": NOW - DAY, "app": "Mumble", "text": "The user said a word.",
                           "call": "call:7", "source": {"quote": "the hair loom swap"}, "heard_in": "Mumble",
                           "channel_known": True, "people": []},
                          {"id": 815, "ref": "journal:815", "at": NOW - H, "app": "Firefox", "text": "Said “seed swop”.",
                           "call": None, "source": {}, "heard_in": "Firefox", "channel_known": False, "people": []}],
                "total": 3}

    def resolve_heard(self, *a, **k):
        return {"ok": True}

    def pin_fact(self, *a, **k):
        return {"ok": True}

    def rules(self):
        return [{"ref": "rule:4", "text": "Don’t call me buddy.", "at": NOW - 3 * DAY, "origin": "user", "kind": "preference"}]

    def calls_in(self, since=None, until=None, limit=30):
        return [{"call_ids": [7], "app": "Mumble", "places": ["Mumble, #seed-swap voice (Allotment Club)"],
                 "people": [{"name": "Noor", "how": "on_call_screen"}, {"name": "noor.plays", "how": "call_chat"},
                            {"name": "Dev", "how": "likely"}]}]


def test_me_shows_memorys_words_one_summary_per_person_and_each_fact_once():
    me = W.build_me(_MeMemory(), now=NOW)
    noor, dev = me["people"]
    assert noor["usual"] == "" and dev["usual"] == "Email, short."                  # a route ref is never shown
    assert noor["summary"] == {"ref": "fact:24", "headline": "Noor runs the seed swap", "text": "She runs the swap on the 18th.",
                               "pinned": False}
    groups = {g["key"]: g for g in me["groups"]}
    refs = [f["ref"] for g in me["groups"] for f in g["facts"]] + [f["ref"] for f in me["routines"] + me["not_routines"]]
    assert len(refs) == len(set(refs)) and "fact:40" not in refs                    # each fact once; under review only there
    assert [it["ref"] for it in me["review"]["items"]] == ["fact:40"] and me["review"]["merged"] == 2
    assert groups["people"]["facts"] == [{"ref": "fact:24", "kind": "person", "h": "Noor runs the seed swap", "person": "person:8",
                                          "pinned": False, "at": NOW - 2 * H, "importance": 4}]   # a link to the dossier
    work = {f["ref"]: f for f in groups["work"]["facts"]}
    assert work["fact:12"]["pinned"] is True and work["fact:12"]["d"] == "You’re building a garden planner."
    # no shown form yet: the fact's own text with only its formats changed, never a rewritten word
    assert work["fact:13"]["d"] == "The user said on 2 October at 7:15 PM they would confirm the tables."
    # said on a call: the place in the one format, who was on it, one friend once (two handles), "maybe" for likely
    assert work["fact:13"]["prov"] == "Heard in Mumble, #seed-swap voice (Allotment Club), 2 Oct, with Noor and maybe Dev"
    assert [r["ref"] for r in me["routines"]] == ["fact:31"] and me["routines"][0]["conf_word"] == "sure"
    assert me["routines"][0]["week"] == [False, False, False, True, False, True, True]
    assert [r["ref"] for r in me["not_routines"]] == ["fact:34"]
    assert groups["about"]["facts"][0]["joke"] is True and me["rules"][0]["prov"].startswith("You told me")
    assert me["can"] == {"pin": True, "review": True, "heard": True}


def test_maybe_misheard_says_where_it_was_heard_in_one_format():
    me = W.build_me(_MeMemory(), now=NOW)
    m = me["misheard"]
    assert m["total"] == 3 and m["direct"] is True
    call, typed = m["items"]
    assert call["quote"] == "the hair loom swap"
    assert call["prov"] == "Heard in Mumble, #seed-swap voice (Allotment Club), yesterday, 9:52 PM, with Noor and maybe Dev"
    assert typed["prov"] == "You said it to me while Firefox was open, today, 8:52 PM"
    unknown = W.heard_line("Discord", {"label": "", "app": "Discord", "people": []}, NOW - DAY, now=NOW)
    assert unknown == "Heard on a Discord call, yesterday, 9:52 PM"


def test_a_call_names_each_friend_once_by_the_name_you_know_them_by():
    lookup = W.name_lookup([{"name": "Noor", "aliases": ["noor.plays"], "routes": []}, {"name": "Dev", "routes": []}])
    people = [{"name": "noor.plays", "how": "call_chat"}, {"name": "Noor", "how": "on_call_screen"},
              {"name": "Dev", "how": "likely"}, {"name": "@NOOR.PLAYS", "how": "likely"}, {"name": "Kai", "how": "on_call_screen"}]
    assert W.call_people(people, lookup) == ["Noor", "maybe Dev", "Kai"]


def test_a_contact_card_lists_the_other_facts_never_the_summary_again():
    card = W.contact_card({"found": True, "usual": "route:3", "routes": [],
                           "facts": [{"ref": "fact:51", "headline": "Climbs on Tuesdays", "text": "She climbs with you on Tuesdays."}]})
    assert card["usual"] == "" and card["facts"] == [{"ref": "fact:51", "h": "Climbs on Tuesdays", "d": "She climbs with you on Tuesdays."}]


# -- Activity ------------------------------------------------------------------------------------------------------------


def test_the_ledger_groups_actions_by_request_and_has_no_undo():
    runs = [{"key": "k1", "lane": "voice", "words": "pause it", "outcome": "final", "ms": 12000, "cost_usd": 0.06, "at": NOW - H},
            {"key": "k2", "lane": "worker", "words": "what's on today", "outcome": "final", "ms": 2000, "at": NOW - 2 * H}]
    rows = [{"id": "run:k1:1", "kind": "action", "at": NOW - H + 1, "what": "Clicked Pause", "status": "failed", "result": "didn’t work",
             "undo": {"todo": "todo:1", "until": NOW + 60}},
            {"id": "run:k1:2", "kind": "action", "at": NOW - H + 2, "what": "Pressed Pause", "status": "verified", "result": "done",
             "cant_undo": True},
            {"id": "run:k2", "kind": "request", "at": NOW - 2 * H, "what": "Answered “what's on today”", "status": "done", "result": "answered"},
            {"id": "c41", "kind": "card", "at": NOW - 3 * H, "what": "The forum has a reply.", "status": "done", "result": "shown",
             "where": "Card · Noticed", "why": "rule: “Tell me when the forum gets a reply.”", "card": "41"}]
    out, groups = W.ledger_groups(rows, runs, NOW)
    assert not any("undo" in r or "cant_undo" in r for r in out)
    assert [r["group"] for r in out] == ["run:k1", "run:k1", "run:k2", "c41"]
    g1, g2, card = groups
    assert g1["title"] == "“pause it”" and g1["said"] == "you said" and g1["steps"] == 2
    assert g1["outcome"] == "Done, but one step didn’t work" and g1["took"] == "12 s" and g1["f"] == "did"
    assert g2["steps"] == 0 and g2["said"] == "you typed" and g2["took"] == "2 s"
    assert card["kind"] == "card" and card["why"].startswith("rule:") and card["card"] == "41"
    assert W.took_words(69) == "1 min 9 s" and W.took_words(0.3) == "1 s" and W.took_words(None) == ""


# -- Work: the Day's sessions and calls ----------------------------------------------------------------------------------


class _DayMemory:
    def __init__(self, day0):
        self.day0 = day0

    def day_timeline(self, since, until):
        def row(s, e, app, proc):
            return {"start": self.day0 + s * H, "end": self.day0 + e * H, "state": "present", "app": app, "process": proc,
                    "active_s": (e - s) * H, "passive_s": 0, "withheld": False, "fullscreen": False, "meeting": False,
                    "by_yuki": False, "title": "", "host": None}
        return [row(10, 11.5, "VS Code", "code"), row(11.5, 12, "Firefox", "firefox"), row(12, 12.2, "Notes", "notes")]

    def activity(self, since=None, until=None, group_by="app", limit=50):
        return {"power": [], "calls": [], "totals": {"present_s": 2.2 * H, "first_at": self.day0 + 10 * H,
                                                     "last_at": self.day0 + 12.2 * H, "switches": 3}}

    def journal_entries(self, since, until=None):
        return []

    def episodes(self, since=None, until=None, limit=50):
        long = "Garden planner: you finished the bed sizes screen and checked it against the balcony beds, then read the forum for a while and made notes."
        return [{"start": datetime.fromtimestamp(self.day0 + 10 * H).isoformat(),
                 "end": datetime.fromtimestamp(self.day0 + 12.2 * H).isoformat(), "text": "10:00-12:12: " + long}]

    def app_colors(self, until=None, days=14):
        return {}

    def calls_in(self, since=None, until=None, limit=30):
        return [{"start": datetime.fromtimestamp(self.day0 + 19 * H).isoformat(), "end": datetime.fromtimestamp(self.day0 + 20 * H).isoformat(),
                 "app": "Mumble", "seconds": 3600, "recorded": True, "places": ["Mumble, #seed-swap voice (Allotment Club)"],
                 "gists": [{"gist": "The user planned the swap.", "shown": "You planned the swap with Noor."}],
                 "people": [{"name": "noor.plays", "how": "call_chat"}, {"name": "Noor", "how": "on_call_screen"}]}]


def test_a_session_opens_to_its_whole_title_and_the_time_in_every_app():
    day = date(2026, 10, 3)
    day0 = datetime.combine(day, datetime.min.time()).timestamp()
    d = W.build_day(_DayMemory(day0), day, now=day0 + 22 * H, week=[],
                    people=[{"name": "Noor", "ids": ["noor", "noor.plays"]}])
    s = d["sessions"][0]
    assert s["title"].endswith("…") and s["full"].startswith("Garden planner: you finished") and not s["full"].endswith("…")
    assert [a[0] for a in s["apps"]] == ["VS Code", "Firefox", "Notes"]
    assert d["first"] == "10 AM" and d["last"] == "12:12 PM"                          # 12-hour outside time columns
    c = d["calls"][0]
    assert c["gists"] == ["You planned the swap with Noor."] and c["people"] == ["Noor"]
    assert c["where"] == "Mumble, #seed-swap voice (Allotment Club)"


# -- the window's actions --------------------------------------------------------------------------------------------------


class _ActMemory:
    def __init__(self):
        self.calls = []

    def pin_fact(self, ref, on=True):
        self.calls.append(("pin", ref, on))
        return {"ok": True, "ref": ref, "pinned": on}

    def resolve_heard(self, ref, verdict, *, correction_id=None):
        self.calls.append(("heard", ref, verdict, correction_id))
        if verdict == "undo":
            return {"ok": False, "error": "Too late to undo: Yuki is already updating memory from your answer."}
        return {"ok": True, "correction_id": 12 if verdict == "wrong" else None}

    def review_mark(self, ref, verdict, *, correction_id=None):
        self.calls.append(("review", ref, verdict, correction_id))
        return {"ok": True, "correction_id": None}

    def day_connector(self, day=None, now=None):
        return {"day": "2026-10-02", "label": "Yesterday", "text": "Yesterday you finished the bed sizes screen.", "memory": "",
                "open": [{"text": "Write the watering timer", "thread": "thread:12"}]}

    def chat_days(self, days=14, now=None):
        return [{"day": "2026-10-02", "count": 3, "first": NOW - DAY, "last": NOW - DAY + 600}]

    def turns_on(self, day, limit=200):
        return [{"id": 9, "at": NOW - DAY, "user": "[Replying to your nudge (kind: notice): \"x\"]\nadd chalk to my list",
                 "reply": "Added  chalk.", "outcome": "final"}]

    def correct_portrait(self, text):
        self.calls.append(("correct", text))
        return 77


def test_pin_resolve_review_and_the_threads_days_go_straight_to_memory(tmp_path):
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = mem = _ActMemory()
    assert win.a_pin({"ref": "fact:12", "on": False}) == {"pinned": False} and mem.calls[-1] == ("pin", "fact:12", False)
    assert win.a_resolve_heard({"ref": "journal:8", "verdict": "wrong"}) == {"correction_id": 12, "verdict": "wrong"}
    late = win.a_resolve_heard({"ref": "journal:8", "verdict": "undo", "correction_id": 12})
    assert late["ok"] is False and late["too_late"] is True and "I’m already updating" in late["error"]   # Yuki says "I"
    assert mem.calls[-1] == ("heard", "journal:8", "undo", 12)
    assert win.a_review_mark({"ref": "fact:40", "verdict": "notmine"})["verdict"] == "notmine"
    assert win.a_review_mark({"ref": "fact:40", "verdict": "delete"})["ok"] is False
    days = win.a_thread_days({})
    assert days["connector"]["text"].startswith("Yesterday you") and days["connector"]["open"] == ["Write the watering timer"]
    assert days["connector"]["day_at"] == datetime(2026, 10, 2, 12).timestamp() and days["days"][0]["count"] == 3
    turns = win.a_chat_day({"day": "2026-10-02"})["turns"]
    assert turns[0]["user"] == "add chalk to my list" and turns[0]["reply"] == "Added chalk."   # Yuki's own block taken off
    assert win.a_day_answer({"verdict": "still", "item": "Write the watering timer"}) == {"sent": False}
    assert not any(c[0] == "correct" for c in mem.calls)                              # "Still on it" sends nothing
    done = win.a_day_answer({"verdict": "done", "item": "Write the watering timer"})
    assert done == {"sent": True, "correction_id": 77} and "Write the watering timer" in mem.calls[-1][1]
    assert "todo_undo" not in web_window.WebWindow.ACTIONS                            # no Undo on Activity


# -- the invented fixture ------------------------------------------------------------------------------------------------


def test_the_fixture_has_the_new_shapes_and_no_third_person_or_route_refs():
    data = web_window.load_fixture()
    me, act = data["pages"]["me"], data["pages"]["activity"]
    text = json.dumps(data["pages"], ensure_ascii=False)
    assert "the user" not in text.casefold() and "route:" not in text
    assert all(p.get("summary") for p in me["people"]) and me["review"]["items"] and me["misheard"]["items"]
    assert {g["key"] for g in me["groups"]} >= {"work", "people", "interests", "prefs", "about"}
    assert act["groups"] and not any("undo" in r for r in act["rows"])
    assert any(s.get("full") for s in data["pages"]["day"]["sessions"])
    assert all(k in data["replies"] for k in ("pin", "resolve_heard", "review_mark", "thread_days", "chat_day", "day_answer"))
