"""Revamp E11-E14 (docs/research/revamp-2026-10-02/32-build-E11-E14.md): the data the Lattice window's new screens
show, wired to memory, and the window's own frame.

* E11 contact rhythm: messages a day with each person, 14 days, both ways (memory's people index + messages);
* E12 thread checklists (milestones landed / open items) and "involves" typed person / app by identity;
* E13 the Playbook page from practice memory's ``workflows`` (procedures, lessons, proposals) and its edits;
* E14 the Activity ledger: each action with time, what, where, why (the user's words, a lesson or procedure it used,
  a rule), result and its Undo window;
* goals on Home's Now strip and on threads;
* the frameless window's non-client hit testing (pure logic) and the idle face's 1 Hz blink tick.

Temp databases and invented people only (Riya, Noor, Theo); nothing touches the desktop or the real memory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from yuki.memory import contacts as C
from yuki.memory import practice as P
from yuki.memory.api import MemoryClient
from yuki.memory.store import Store, WorkOp

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 3, 12, 0).timestamp()
DAY = 86400.0


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    try:
        yield s
    finally:
        s.close()


# -- helpers: chats, people, threads -----------------------------------------------------------------------------


def _msg(store: Store, thread_id: int, n: int, *, sender: str | None, me: bool, at: float,
         first_seen: float | None = None) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label, at,"
        " text_ciphertext, first_seen, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, None, store.cipher.digest("fp", str(thread_id), str(n)), store.cipher.digest("ck", str(n)),
         store._enc(sender), int(me), None, at, store._enc(f"m{n}"), first_seen if first_seen is not None else at,
         "new")))


def _thread(store: Store, app: str, process: str, scope: str) -> int:
    return store.upsert_thread(app, scope, f"https://chat.example/{scope}", process=process, scope=scope,
                               kind="conversation")


def _contact(store: Store, name: str, routes: list[dict[str, Any]], aliases: tuple[str, ...] = ()) -> int:
    def op(conn: Any) -> int:
        cid = int(conn.execute(
            "INSERT INTO contacts(name_key, name_ciphertext, relation_ciphertext, aliases_ciphertext, tone_ciphertext,"
            " fact_ids, run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (C.name_key(store, name), store.cipher.encrypt(name), None,
             store._enc(json.dumps([{"name": a, "how": "seen"} for a in aliases])), None, "[]", None, NOW, NOW)).lastrowid)
        for r in routes:
            conn.execute(
                "INSERT INTO contact_routes(contact_id, route_key, service, kind, chat_ciphertext, handle_ciphertext,"
                " thread_ids, evidence, run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, C.route_key(store, r["service"], r["chat"]), r["service"], r["kind"], store.cipher.encrypt(r["chat"]),
                 store._enc(r.get("handle")), json.dumps(r["threads"]), "[]", None, NOW, NOW))
        return cid

    cid = store._write(op)
    C._invalidate(store)
    return cid


def _day_at(days_ago: int, hour: int = 10) -> float:
    d = datetime.fromtimestamp(NOW).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, 0).timestamp()


def rhythm_world(store: Store) -> dict[str, int]:
    """Riya: a DM (both ways) and a climbing group where she, Noor and the user write; memory began reading chats 6
    days ago, and one scrolled-in old message sits 10 days back (before memory watched: not counted)."""
    t = {"dm": _thread(store, "Signal", "signal.exe", "Riya"), "grp": _thread(store, "Telegram", "telegram.exe", "Crew")}
    n = 0
    plan = [(6, True, 2), (6, False, 1), (3, True, 4), (3, False, 5), (1, False, 2), (0, True, 3), (0, False, 3)]
    for days_ago, me, count in plan:
        for k in range(count):
            n += 1
            _msg(store, t["dm"], n, sender=None if me else "Riya", me=me, at=_day_at(days_ago, 10 + k % 8))
    for days_ago, sender, me, count in [(3, "riyaclimbs", False, 2), (3, "Noor", False, 6), (3, "You", True, 9),
                                        (0, "Riya", False, 1)]:
        for k in range(count):
            n += 1
            _msg(store, t["grp"], n, sender=sender, me=me, at=_day_at(days_ago, 12 + k % 6))
    n += 1   # history scrolled into view today: dated 10 days back, before memory began watching
    _msg(store, t["dm"], n, sender="Riya", me=False, at=_day_at(10), first_seen=_day_at(0, 9))
    t["riya"] = _contact(store, "Riya", [
        {"service": "signal", "kind": "dm", "chat": "Riya", "threads": [t["dm"]]},
        {"service": "telegram", "kind": "group", "chat": "Crew", "handle": "riyaclimbs", "threads": [t["grp"]]}])
    t["theo"] = _contact(store, "Theo", [])
    return t


# -- E11: contact rhythm -------------------------------------------------------------------------------------------


def test_rhythm_counts_dm_messages_both_ways_and_only_what_they_wrote_in_a_group(store):
    t = rhythm_world(store)
    people = {c.name: c for c in C.people(store)}
    r = C.rhythm(store, list(people.values()), now=NOW)[t["riya"]]
    # 14 days, today last; the 7 days before memory began watching (6 days ago) are unknown, not zero
    assert r["days"][:7] == [None] * 7
    #            6 ago      5  4  3 ago (dm 9 + her 2 in the group)  2  1 ago  today (dm 6 + her 1 in the group)
    assert r["days"][7:] == [3, 0, 0, 11, 0, 2, 7]
    assert r["total"] == 23 and r["known_days"] == 7 and r["active_days"] == 4
    assert r["mine"] == 9 and r["theirs"] == 14          # Noor's 6 and the user's 9 group messages are not hers
    assert r["peak"] == 11 and r["peak_date"] == (datetime.fromtimestamp(NOW).date() - timedelta(days=3)).isoformat()
    assert r["since_watch"] is True and r["dates"][-1] == datetime.fromtimestamp(NOW).date().isoformat()
    theo = C.rhythm(store, [people["Theo"]], now=NOW)[t["theo"]]
    assert theo["total"] == 0 and theo["days"][-1] == 0 and theo["days"][0] is None


def test_rhythm_reaches_the_people_list_and_the_contact_card(store, tmp_path):
    t = rhythm_world(store)
    client = MemoryClient(store)
    rows = {p["name"]: p for p in client.people_list()}
    assert rows["Riya"]["rhythm"]["total"] == C.rhythm(store, C.people(store))[t["riya"]]["total"]
    assert len(rows["Riya"]["rhythm"]["days"]) == 14 and "rhythm" in rows["Theo"]
    card = client.contact("Riya")
    assert card["found"] and card["rhythm"]["days"] == rows["Riya"]["rhythm"]["days"]
    # a new group message from her is counted on the next read (the sender cache is incremental)
    _msg(store, t["grp"], 999, sender="Riya", me=False, at=time.time() - 60)
    assert client.contact("Riya")["rhythm"]["total"] == rows["Riya"]["rhythm"]["total"] + 1


# -- E12: checklists and what a thread involves ---------------------------------------------------------------------


def _work(store: Store, title: str, open_items: list[str], entities: list[str]) -> int:
    op = WorkOp("starts", None, title=title, state="half done", open_items=open_items, entities=entities, at=NOW - DAY)
    store.apply_work([op])
    return int(op.result_id)


def _milestone(store: Store, work_id: int, at: float, *, by_yuki: bool = False, superseded: bool = False,
               milestone: bool = True) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO journal(at, thread_id, app, fact_ciphertext, importance, created_at, by_yuki, milestone,"
        " work_thread_id, superseded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (at, None, "Code", store.cipher.encrypt("shipped a part"), 3, at, int(by_yuki), int(milestone), work_id,
         at + 5 if superseded else None)))


def test_a_threads_checklist_is_milestones_landed_over_those_plus_its_open_items(store):
    from yuki.memory.workthreads import checklists

    w = _work(store, "Garden planner", ["Write the watering schedule", "Test on the old laptop"], ["Firefox"])
    for k in range(3):
        _milestone(store, w, NOW - 3600 * (k + 1))
    _milestone(store, w, NOW - 60, by_yuki=True)          # Yuki's own words: not the user's progress
    _milestone(store, w, NOW - 50, superseded=True)       # replaced by a newer reading
    _milestone(store, w, NOW - 40, milestone=False)       # a plain fact
    empty = _work(store, "Budget", [], [])
    threads = {t.id: t for t in store.work_threads(None)}
    got = checklists(store, list(threads.values()))
    assert got[w] == {"done": 3, "open": 2, "total": 5, "next": "Write the watering schedule",
                      "last_done_at": NOW - 3600}
    assert got[empty] == {"done": 0, "open": 0, "total": 0, "next": None, "last_done_at": None}
    row = next(r for r in MemoryClient(store).threads(statuses=None, limit=10) if r["id"] == w)
    assert row["checks"]["done"] == 3 and row["checks"]["total"] == 5


def test_involves_is_typed_person_or_app_by_identity_never_by_a_word_inside(store):
    rhythm_world(store)                                       # Riya (alias riyaclimbs) is in the people index
    _thread(store, "Google Chrome", "chrome.exe", "a page")   # an app memory knows
    w = _work(store, "Seed swap", [], ["Riya", "RIYACLIMBS", "chrome", "Riya's notes", "seed list.xlsx"])
    row = next(r for r in MemoryClient(store).threads(statuses=None, limit=10) if r["id"] == w)
    typed = {e["name"]: e["type"] for e in row["involves_typed"]}
    assert typed == {"Riya": "person", "RIYACLIMBS": "person", "chrome": "app", "Riya's notes": "thing",
                     "seed list.xlsx": "thing"}
    assert {e["person"] for e in row["involves_typed"] if e["type"] == "person"} == {"Riya"}
    assert row["involves"] == ["Riya", "RIYACLIMBS", "chrome", "Riya's notes", "seed list.xlsx"]   # unchanged


# -- E14: the run ledger -------------------------------------------------------------------------------------------


def _run(store: Store, words: str, steps: list[dict[str, Any]], *, at: float, outcome: str = "final",
         rid: str = "r1", lane: str = "voice") -> dict:
    return P.record_run(store, {"source": "agent", "session_id": "s", "request_id": rid, "lane": lane, "at": at,
                                "ended_at": at + 9, "words": words, "reply": "done", "outcome": outcome,
                                "steps": steps, "cost_usd": 0.05, "model_calls": 2, "wall_s": 9.0}, now=at + 10)


def test_the_run_ledger_lists_acting_steps_with_the_playbooks_they_used(store):
    proc = P.add_playbook(store, {"kind": "procedure", "intent": "Restore the browser after a restart",
                                  "apps": ["firefox"], "body": {"steps": ["Open it", "Click Restore"]}})
    lesson = P.add_playbook(store, {"kind": "pitfall", "intent": "Pause music only with the player in front",
                                    "apps": ["music"], "body": {"text": "a background click is dropped",
                                                                "instead": "bring it forward first"}})
    _thread(store, "Firefox", "firefox.exe", "tabs")
    at = time.time() - 600
    _run(store, "bring my tabs back", [
        {"tool": "look_at_window", "kind": "read", "ok": True, "at": at + 1},
        {"tool": "launch_app", "kind": "action", "app": "firefox.exe", "input": {"name": "firefox"}, "ok": True,
         "verdict": True, "result": "Firefox is open", "ms": 900, "at": at + 2, "playbook": f"pb:{proc}"},
        {"tool": "click", "kind": "action", "app": "firefox.exe", "target": "button \"Restore\"", "ok": True,
         "verdict": None, "result": "clicked", "ms": 300, "at": at + 3, "playbook": f"pb:{lesson}"},
        {"tool": "todo", "kind": "memory", "input": {"action": "add", "text": "Order seed trays"}, "ok": True,
         "result": "to-do added", "at": at + 4},
        {"tool": "ask_user", "kind": "ask", "ok": True, "at": at + 5}], at=at)
    rows = MemoryClient(store).run_ledger(since=at - 60)
    assert len(rows) == 1 and rows[0]["words"] == "bring my tabs back" and rows[0]["lane"] == "voice"
    steps = rows[0]["steps"]
    assert [s["tool"] for s in steps] == ["launch_app", "click", "todo"]          # reads and questions left out
    assert steps[0]["cited"] == [{"ref": f"pb:{proc}", "kind": "procedure", "title": "Restore the browser after a restart"}]
    assert steps[1]["cited"][0]["kind"] == "pitfall" and steps[1]["target"] == "button \"Restore\""
    assert steps[0]["app_name"] == "Firefox" and steps[0]["verdict"] is True and steps[1]["verdict"] is None
    assert json.loads(steps[2]["input"]) == {"action": "add", "text": "Order seed trays"}
    assert MemoryClient(store).run_ledger(since=at + 60) == []


# -- the window's data: rhythm words, Work, Playbook, ledger, goals ------------------------------------------------

from yuki.ui import web_data as W  # noqa: E402


def test_a_person_row_carries_the_rhythm_figure_and_the_words_around_it(store):
    t = rhythm_world(store)
    client = MemoryClient(store)
    row = next(W.person_from_index(p) for p in client.people_list() if p["name"] == "Riya")
    assert row["rhythm"] == C.rhythm(store, C.people(store))[t["riya"]]["days"] and len(row["rhythm"]) == 14
    info = row["rhythm_info"]
    today = datetime.fromtimestamp(time.time()).date()
    label = lambda d: f"{d.day} {d:%b}"  # noqa: E731
    known = [v for v in row["rhythm"] if v is not None]
    assert info["start_label"] == label(today - timedelta(days=13)) and info["known"] == len(known)
    assert info["total"] == sum(known) and info["peak"] == max(known)
    dm = next(r for r in row["routes"] if r["kind"] == "dm")
    assert dm["label"] == "DM" and dm["sub"] == "@Riya"                      # the design's "DM @handle"
    grp = next(r for r in row["routes"] if r["kind"] == "group")
    assert grp["label"] == "Crew" and grp["sub"] == "riyaclimbs"
    card = W.contact_card(client.contact("Riya"))
    assert card["rhythm"] == row["rhythm"] and card["rhythm_info"]["total"] == info["total"]
    assert "rhythm" not in W.person_from_index({"name": "X", "routes": []})   # an older memory: no figure


def test_work_shows_the_checklist_people_apps_and_goal_from_memory(store):
    rhythm_world(store)
    _thread(store, "Google Chrome", "chrome.exe", "a page")
    w = _work(store, "Seed swap", ["Count the packets", "Reply on the forum"], ["RIYACLIMBS", "chrome", "seed list.xlsx"])
    _milestone(store, w, NOW - 600)

    class Mem(MemoryClient):
        def goals(self, scope=None, statuses=("open", "unknown"), today=False, limit=30):
            return [{"id": "G2", "thread": f"W{w}", "text": "bring 20 packets", "status": "open"},
                    {"id": "G1", "thread": f"W{w}", "text": "an older goal", "status": "open"},
                    {"id": "G3", "thread": None, "text": "a day goal", "status": "open"}]

    data = W.build_work(Mem(store), week={"days": [], "totals": {}})
    t = next(x for x in data["threads"] if x["title"] == "Seed swap")
    assert t["checks"] == {"done": 1, "total": 3, "next": "Count the packets"}
    assert t["people"] == ["Riya"] and t["apps"] == ["chrome", "seed list.xlsx"]   # the person once, by index name
    assert t["goal"] == "bring 20 packets"
    assert W.thread_checks({"checks": {"done": 0, "open": 0, "total": 0}}) is None


def test_home_has_todays_goals_open_first_on_the_now_strip():
    class Mem:
        def __getattr__(self, name):
            return lambda *a, **k: {} if name in ("status", "call_status", "call_audio_waiting") else []

        def goals(self, scope=None, statuses=None, today=False, limit=30):
            assert today and "done" in statuses
            return [{"id": "G1", "text": "send the invoice", "status": "done"},
                    {"id": "G2", "words": "finish the  schedule", "text": "", "status": "open", "thread": "W3"},
                    {"id": "G3", "text": "", "status": "open"}]

    d = W.build_home(Mem(), requests_csv=None)
    assert d["now"]["goals"] == [{"ref": "G2", "text": "finish the schedule", "status": "open", "done": False, "thread": "W3"},
                                 {"ref": "G1", "text": "send the invoice", "status": "done", "done": True, "thread": None}]
    assert W.goals_today(object()) == []                                            # a memory without goals


def test_the_playbook_page_is_practice_memorys_workflows(store):
    proc = P.add_playbook(store, {"kind": "procedure", "intent": "Restore the browser after a restart", "apps": ["firefox"],
                                  "body": {"steps": ["Open it", "Click Restore"]}, "origin": "distill"},
                          events=[{"type": "observed", "run_key": "a"}, {"type": "observed", "run_key": "b"},
                                  {"type": "succeeded", "run_key": "b"}])   # appLearn: a way rests on a success
    P.add_playbook(store, {"kind": "pitfall", "intent": "Pause the music", "apps": ["music"], "origin": "postmortem",
                           "body": {"text": "a click behind another window went nowhere", "instead": "bring it forward first"},
                           "avoid": "clicking a background window"},
                   events=[{"type": "taught", "run_key": "c", "refs": {"words": "bring it forward"}}])
    P.add_playbook(store, {"kind": "style", "intent": "Write emails to the team", "origin": "postmortem",
                           "body": {"text": "In English, short."}},
                   events=[{"type": "observed", "run_key": "e"},
                           {"type": "taught", "run_key": "e", "refs": {"quote": "in English please"}}])
    cand = P.add_playbook(store, {"kind": "habit", "intent": "Videos in Firefox", "apps": ["firefox"],
                                  "body": {"text": "You watch videos in Firefox."}, "origin": "miner"},
                          events=[{"type": "observed_day", "run_key": "miner:site"}])
    d = W.build_playbook(MemoryClient(store))
    assert d["workflows_available"] and d["know_how"] == []
    p = next(x for x in d["procedures"] if x["ref"] == f"pb:{proc}")
    assert p["title"] == "Restore the browser after a restart" and p["steps"] == ["Open it", "Click Restore"]
    assert p["app"] == "Firefox" and p["uses"] == 0 and p["conf"] is not None and p["conf_word"]
    assert p["learned"].startswith("From a run that worked")
    # appLearn: the pitfall is a quiet note for Yuki, never on the page; lessons are the user's style notes
    assert [x["dont"] for x in d["lessons"]] == ["Write emails to the team"]
    assert d["lessons"][0]["because"] == "In English, short." and d["lessons"][0]["from"].startswith("In your words")
    assert "Pause the music" not in json.dumps(d)
    prop = next(x for x in d["proposals"] if x["ref"] == f"pb:{cand}")
    assert prop["title"] == "Videos in Firefox" and "Seen" in prop["basis"] and prop["app"] == "Firefox"
    # more things seen once than NEW blocks: the rest stay visible in their own lists, marked tentative
    for n in range(2):
        P.add_playbook(store, {"kind": "procedure", "intent": f"Open the planner {n}", "body": {"steps": ["a", "b"]}},
                       events=[{"type": "observed", "run_key": f"x{n}"}, {"type": "succeeded", "run_key": f"x{n}"}])
    d = W.build_playbook(MemoryClient(store))
    assert len(d["proposals"]) == W.PROPOSALS_SHOWN == 2
    tentative = [p for p in d["procedures"] + d["choices"] if p.get("tentative")]   # a habit is an app choice (appWin2 W7)
    assert len(tentative) == 1 and tentative[0]["conf_word"] == "tentative, seen once"


def test_save_edit_and_no_map_onto_memorys_own_playbook_actions(store, tmp_path):
    from yuki.ui import web_window
    from yuki.ui.web_window import workflow_action

    assert workflow_action("pb:3", "save") == "save" and workflow_action("pb:3", "no") == "forget"
    assert workflow_action("pb:3", "edit", "in my words") == "edit" and workflow_action("pb:3", "edit", "") is None
    assert workflow_action("question:4", "edit", "the DM") == "save" and workflow_action("question:4", "no") == "disable"
    assert workflow_action("question:4", "save", "") is None and workflow_action("pb:3", "delete") is None
    pid = P.add_playbook(store, {"kind": "procedure", "intent": "Open the planner", "body": {"steps": ["a", "b"]}})
    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.memory = MemoryClient(store)
    assert win.a_workflow_edit({"ref": f"pb:{pid}", "verb": "edit", "words": "open it from the start menu"})["queued"]
    assert [w for _o, w, _at in P.pending_edits(store)] == ["open it from the start menu"]   # re-distilled later
    assert win.a_workflow_edit({"ref": f"pb:{pid}", "verb": "no"})["ok"]
    assert P.get(store, pid).valid_to is not None                                    # retired, history kept
    assert not win.a_workflow_edit({"ref": f"pb:{pid}", "verb": "edit", "words": ""})["ok"]


def test_the_trays_playbook_ops_reach_memory_the_same_way(store):
    from types import SimpleNamespace

    from yuki.ui.window_host import WindowHost

    pid = P.add_playbook(store, {"kind": "procedure", "intent": "Open the planner", "body": {"steps": ["a", "b"]}})
    client = MemoryClient(store)
    logged = []
    fake = SimpleNamespace(_memory=lambda: SimpleNamespace(call=lambda what, fn, timeout_s: fn(client)),
                           _log=lambda kind, **f: logged.append(f))
    page = WindowHost._op_workflows(fake, {})
    assert page["available"] and [c["id"] for c in page["procedures"] + page["proposals"]] == [f"pb:{pid}"]
    assert WindowHost._op_workflow_edit(fake, {"ref": f"pb:{pid}", "verb": "save"})["ok"]
    assert WindowHost._op_workflow_edit(fake, {"ref": f"pb:{pid}", "verb": "edit"})["ok"] is False   # words needed
    assert logged == [{"op": "workflow_edit", "action": "save", "ok": True}]


def test_the_ledger_says_why_in_the_users_words_with_lessons_procedures_and_rules(tmp_path):
    now = time.time()
    run_at = now - 300   # five minutes ago: the to-do it added can still be undone
    steps = [{"step": 1, "at": run_at + 2, "tool": "launch_app", "kind": "action", "app": "firefox", "app_name": "Firefox",
              "target": "", "input": json.dumps({"query": "firefox"}), "ok": True, "verdict": True, "expected": False,
              "why": "", "result": "open", "ms": 900, "cited": [{"ref": "pb:1", "kind": "procedure", "title": "Restore Firefox"}]},
             {"step": 2, "at": run_at + 4, "tool": "click", "kind": "action", "app": "music", "app_name": "Music",
              "target": "button \"Pause\"", "input": "{}", "ok": False, "verdict": None, "expected": False, "why": "",
              "result": "the window wasn't in front", "ms": 300,
              "cited": [{"ref": "pb:2", "kind": "pitfall", "title": "Pause the music"}]},
             {"step": 3, "at": run_at + 6, "tool": "todo", "kind": "memory", "app": None, "app_name": "", "target": "",
              "input": json.dumps({"action": "add", "text": "Water the basil"}), "ok": True, "verdict": None,
              "expected": False, "why": "", "result": "to-do added", "ms": 20, "cited": []}]
    calls_rows = [{"app": "Signal", "start": now - 7200, "end": now - 3600, "ongoing": False, "seconds": 3600,
                   "speech_s": 600, "recorded": True, "consent": "always", "gists": [{"call_id": 1, "gist": "Planned it."}],
                   "people": []},
                  {"app": "Meet", "start": now - 5000, "end": now - 4000, "ongoing": False, "seconds": 1000,
                   "recorded": False, "gists": [], "people": []}]

    class Mem:
        def run_ledger(self, since=None, until=None, limit=80):
            return [{"key": "agent:s:worker-3", "source": "agent", "lane": "voice", "at": run_at, "ended_at": run_at + 9,
                     "words": "pause it and remind me to water the basil", "outcome": "final", "cost_usd": 0.06,
                     "ms": 9000, "calls": 3, "steps": steps},
                    {"key": "agent:s:worker-4", "source": "agent", "lane": "worker", "at": run_at + 100,
                     "words": "what's on today", "outcome": "final", "cost_usd": 0.01, "ms": 3000, "steps": []}]

        def todos(self, include_done=False):
            return [{"id": "todo:7", "text": "Water the basil", "source": "user", "status": "open",
                     "since": datetime.fromtimestamp(run_at + 7).isoformat()}]

        def standing_requests(self, include_ended=False):
            return [{"ref": "request:3", "quote": "Tell me when the forum thread gets a reply."}]

        def recent_nudges(self, hours=12):
            return [{"id": 41, "kind": "notice", "text": "The forum thread has a reply.", "shown_at": now - 300,
                     "ref": "request:3", "reason": "standing request R3 matched: a reply", "reaction": "shown"},
                    {"id": 42, "kind": "notice", "text": "Next: the schedule.", "shown_at": now - 200, "ref": None,
                     "reason": "you stopped mid-way", "reaction": "shown"}]

        def activity(self, **k):
            return {"items": []}

        def calls_in(self, since=None, until=None, limit=30):
            return calls_rows

    csv_path = tmp_path / "requests.csv"
    csv_path.write_text(
        "timestamp,session,lane,request,wall_s,tool_calls,cost_usd,outcome\n"
        f"{datetime.fromtimestamp(run_at).isoformat(timespec='seconds')},s,worker,pause it,9,click=1,0.06,final\n"
        f"{datetime.fromtimestamp(now - 3 * 3600).isoformat(timespec='seconds')},s,worker,open my budget,3,,0.03,final\n",
        encoding="utf-8")
    d = W.build_activity(Mem(), requests_csv=csv_path, now=now)
    rows = {r["id"]: r for r in d["rows"]}
    launch, click, todo = (rows[f"run:agent:s:worker-3:{i}"] for i in (1, 2, 3))
    assert launch["what"] == "Opened Firefox" and launch["where"] == "Firefox" and launch["f"] == "did"
    assert launch["why"] == "you said “pause it and remind me to water the basil”, using how you do it"
    assert launch["chips"] == [{"kind": "memory", "text": "Restore Firefox"}] and launch["status"] == "verified"
    # plain words only: no timing, no raw failure text, no "can't undo" (appC1 review F5)
    assert launch["result"] == "done, checked on screen" and "cant_undo" not in launch
    assert click["f"] == "failed" and click["status"] == "failed" and click["result"] == "didn’t work"
    # appLearn: a pitfall is Yuki's own quiet note, never shown as something learned (no chip, no "lesson" words)
    assert click["chips"] == [] and "lesson" not in click["why"]
    # the to-do's own words, from memory's record of it (appC1 review F9), never from the step's input; no Undo on
    # the Activity tab (the 3 Oct ask A)
    assert todo["what"] == "Added to your list “Water the basil”" and "undo" not in todo
    # the steps are grouped under the request they belonged to (3 Oct ask C)
    g = next(x for x in d["groups"] if x["id"] == "run:agent:s:worker-3")
    assert g["title"] == "“pause it and remind me to water the basil”" and g["steps"] == 3 and g["said"] == "you said"
    assert g["outcome"] == "Done, but one step didn’t work" and g["took"] == "9 s" and g["f"] == "did"
    assert {launch["group"], click["group"], todo["group"]} == {"run:agent:s:worker-3"}
    answered = rows["run:agent:s:worker-4"]
    assert answered["what"] == "Answered “what's on today”" and answered["why"] == "you typed “what's on today”"
    reqs = [r for r in d["rows"] if r["kind"] == "request" and r["id"].startswith("req:")]   # request log rows
    assert [r["what"] for r in reqs] == ["“open my budget”"]          # the traced request is not listed twice
    assert rows["c41"]["why"] == "rule: “Tell me when the forum thread gets a reply.”"
    assert rows["c42"]["why"] == "you stopped mid-way"
    call = next(r for r in d["rows"] if r["kind"] == "call")
    assert call["why"] == "rule: always for Signal calls" and call["f"] == "calls" and call["result"] == "turned into text"
    assert len([r for r in d["rows"] if r["kind"] == "call"]) == 1   # an unrecorded meeting is nothing Yuki did
    assert d["counts"]["did_today"] >= 4 and d["counts"]["no_reason"] == 0


# -- the frameless window --------------------------------------------------------------------------------------------


def test_the_title_bar_answers_windows_hit_test_like_a_real_one():
    from yuki.ui import webview2 as V

    lay = V.CaptionLayout.from_page({"h": 34, "dpr": 1.5, "buttons": {"min": [1308, 0, 44, 34], "max": [1352, 0, 44, 34],
                                                                       "close": [1396, 0, 44, 34], "bogus": [0, 0, 1, 1]},
                                     "no_drag": [[300, 4, 40, 20], "x"]}, 1.0)
    assert lay.height == 51 and lay.buttons["max"] == (2028, 0, 66, 51) and set(lay.buttons) == {"min", "max", "close"}
    assert lay.top == 8 and lay.corner == 18 and lay.no_drag == [(450, 6, 60, 30)]
    width = 2160
    hit = lambda x, y, maxed=False: V.caption_hit(x, y, width, lay, maximized=maxed)  # noqa: E731
    assert hit(2050, 25) == V.HTMAXBUTTON == 9                     # snap layouts appear over this one
    assert hit(1980, 25) == V.HTMINBUTTON and hit(2140, 30) == V.HTCLOSE
    assert hit(800, 25) == V.HTCAPTION and hit(470, 15) == V.HTCLIENT   # a page control inside the bar
    assert hit(800, 3) == V.HTTOP and hit(5, 3) == V.HTTOPLEFT and hit(2155, 3) == V.HTTOPRIGHT
    assert hit(800, 3, maxed=True) == V.HTCAPTION                  # maximized: no top edge to resize
    assert hit(800, 200) == V.HTCLIENT
    assert hit(2050, 3) == V.HTTOP                                 # the very top edge above a button still resizes
    assert V.CaptionLayout.from_page({"h": "bad", "buttons": None}, 1.0).height == 34


def test_the_pages_frame_messages_reach_the_host_on_the_ui_thread(tmp_path):
    from yuki.ui import web_window

    calls = []

    class Host:
        def set_caption(self, msg):
            calls.append(("caption", msg.get("h")))

        def window_op(self, op):
            calls.append(("window", op))
            return True

        def post(self, m):
            pass

    win = web_window.WebWindow(db_path=tmp_path / "x.db", connect=False)
    win.host = Host()
    win.on_message({"op": "caption", "h": 34, "buttons": {}})
    win.on_message({"op": "window", "do": "drag"})
    win.on_message({"op": "window", "do": "maximize"})
    assert calls == [("caption", 34), ("window", "drag"), ("window", "maximize")]


@pytest.mark.skipif(__import__("os").environ.get("YUKI_WEBVIEW2_TEST") != "1",
                    reason="opens a real (off-screen) WebView2 window")
def test_a_real_offscreen_window_has_the_frameless_frame_and_answers_windows_hit_test(tmp_path):
    import ctypes
    import os
    import sys

    metrics = tmp_path / "m.json"
    before = ctypes.windll.user32.GetForegroundWindow()
    proc = subprocess.run([sys.executable, "-m", "yuki.ui.web_window", "--fixture", "--offscreen", "--no-connect",
                           "--profile", str(tmp_path / "profile"), "--metrics", str(metrics), "--exit-after", "4",
                           "--probe-frame", "--t0", repr(time.time())], env={**os.environ, "PYTHONPATH": str(ROOT)},
                          timeout=90)
    assert proc.returncode == 0
    m = json.loads(metrics.read_text(encoding="utf-8"))
    f = m["frame"]
    assert f["thick_frame"] and f["caption_style"] and f["maximize_box"]        # snap, resize, min/max animations
    assert f["client_origin"][1] == 0 and f["client_origin"][0] > 0              # no title bar; side borders kept
    p = f["points"]
    assert p["max_button"]["nchittest"] == 9 and p["close_button"]["nchittest"] == 20 and p["min_button"]["nchittest"] == 8
    assert p["title_bar"]["nchittest"] == 2 and p["top_edge"]["nchittest"] == 12 and p["page"]["nchittest"] == 1
    assert p["left_border"]["nchittest"] == 10
    assert m["took_focus"] is False and ctypes.windll.user32.GetForegroundWindow() == before


def test_the_frame_is_built_into_the_page_without_markup_from_data():
    app = (ROOT / "yuki" / "ui" / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "yuki" / "ui" / "web" / "lattice.css").read_text(encoding="utf-8")
    for part in ("titlebar", "dragbar", "resize-top", "B.send('caption'", "B.send('window'", "data-hover"):
        assert part in app or part in css, part
    assert "app-region: drag" in css and ".titlebar button:hover, .titlebar button[data-hover]" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_idle_face_blinks_on_a_1hz_tick_with_two_still_draws_and_no_loop():
    r = subprocess.run(["node", str(ROOT / "scripts" / "check_blink_tick.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout.strip().splitlines()[-1])
    idle = m["idle"]
    assert idle["loopTicks"] == 0                                  # never the 30 fps loop for an idle blink
    assert 8 <= idle["blinks"] <= 15 and idle["draws"] == 2 * idle["blinks"]
    assert all(4 <= g <= 7 for g in idle["gaps"])                   # every 4-7 s, as the design's 3.8-7 s
    assert m["hidden"] == {"blinks": 0, "draws": 0} and m["reduced"] == {"blinks": 0, "draws": 0}
    assert m["working"]["blinks"] == 0 and m["gone"] == {"faces": 0, "timer": 0}
