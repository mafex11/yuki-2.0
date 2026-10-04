"""appOpen F3 (5 Oct): learn from the user's own fix.

Live 5 Oct: Yuki's "open Valorant" showed the launcher's tray window and was judged "worked" at once; the user said the
game was not there and started it themselves (Start, the launcher's window, the game full screen). Nothing was learned.
Now: the opening keeps its watch's outcome, the learned run is looked at again once its follow-up window has passed,
the user's own activity after it (timeline rows, launch facts, Yuki's own effect marked) goes to the post-mortem, and a
way the user showed - judged the same goal by the model, checked in code - is written through the practice pipeline,
the user's outcome being its verification. launch_app reads back the program it learned.

Offline: temp databases, invented apps (a launcher "Fake Launcher" and a game "Starfall" started through it), a
word-bag embedder and a stand-in for the model; nothing touches the desktop, the real memory or the network.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from test_c_practice import NOW, Worker, store, vec  # noqa: F401 - the store fixture
from yuki.memory import lineage
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory.api import MemoryClient
from yuki.memory.store import TimelineRow

CARRIER = r"C:\Games\Fake Launcher\fakelauncher.exe"
BOOT = r"C:\Games\Starfall\starfall.exe"
GAME = r"C:\Games\Starfall\bin\starfall-win64-shipping.exe"
WORDS = "open starfall"
END = NOW + 181.0     # the watch ended (180 s) without the game's window


def opening_run(request_id: str = "voice-s1-3-h1", *, watched: bool | None = False) -> dict[str, Any]:
    """The voice hand's opening as the hands keep it after the watch (yuki.voice.hands._trace_opening)."""
    steps = [
        {"tool": "launch_app", "kind": "action", "app": None, "target": 'app "Starfall"', "input": {"name": "Starfall"},
         "ok": True, "verdict": None, "why": "still starting: no window of it yet when the hand returned",
         "result": "started 'Starfall' the way the Start menu does: Fake Launcher took it; no process of Starfall yet",
         "ms": 900.0, "at": NOW},
        {"tool": "launch_app", "kind": "action", "app": "starfall-win64-shipping" if watched else None,
         "target": 'app "Starfall", watched', "input": {"name": "Starfall", "watch": True},
         "ok": watched is not False, "verdict": watched,
         "why": "its own window showed" if watched else "its own window did not show",
         "result": ("Starfall's own window is up" if watched else "no window of Starfall within 180 s; in front "
                    "instead: fakelauncher ux.exe \"Fake Launcher\", Fake Launcher's own window, not Starfall"),
         "ms": 180000.0, "at": NOW + 1.0},
    ]
    return {"source": "hand", "session_id": "S5", "request_id": request_id, "lane": "voice", "at": NOW,
            "ended_at": END, "words": WORDS, "reply": steps[1]["result"],
            "outcome": "final" if watched else "unverified", "steps": steps, "wall_s": 181.0}


def timeline(store: Any) -> None:
    """What was in front after it: the launcher's window the opening brought up (Yuki's effect), then the user's own
    Start, the launcher's window, and the game full screen."""
    rows = [
        (NOW + 0.5, 100.0, "fakelauncher ux", "Fake Launcher", "Fake Launcher", False),
        (NOW + 250.0, 2.0, "searchhost", "Search", "Start", False),
        (NOW + 262.0, 15.0, "fakelauncher ux", "Fake Launcher", "Fake Launcher", False),
        (NOW + 330.0, 120.0, "starfall-win64-shipping", "Starfall", "STARFALL", True),
    ]
    for start, active, process, app, title, full in rows:
        store.save_timeline(TimelineRow(id=None, started_at=start, ended_at=start + active, state="present",
                                        process=process, app=app, title=title, active_s=active, fullscreen=full))


@pytest.fixture(autouse=True)
def launches() -> Any:
    lineage.clear()
    lineage.add({"at": NOW + 330.0, "image": "starfall-win64-shipping.exe", "exe": GAME, "started": NOW + 328.0,
                 "parents": [{"image": "starfall.exe", "exe": BOOT, "started": NOW + 327.0},
                             {"image": "fakelauncher.exe", "exe": CARRIER, "started": NOW - 86400.0}],
                 "by_yuki": False})
    lineage.set_start_entries({CARRIER: [{"name": "Starfall", "arguments": "--launch-product=starfall"},
                                         {"name": "Fake Launcher", "arguments": ""}]})
    yield
    lineage.clear()
    lineage._entries = None


PIT = {"op": "ADD", "kind": "pitfall", "intent": "open Starfall", "apps": [], "from": [1, 2],
       "text": "the opening left Fake Launcher's own window in front; the game never came up",
       "instead": "open the Start entry Starfall and wait for the game's own window",
       "family": "opening a game that starts through a launcher"}
SHOWN = {"op": "ADD", "kind": "procedure", "intent": "open Starfall", "from_user": ["u2", "u3", "u4"],
         "entry": "Starfall", "steps": [{"do": "Open the Start entry Starfall: Fake Launcher starts the game",
                                         "from": []},
                                        {"do": "Wait for the game's own window, full screen", "from": []}],
         "family": "opening a game that starts through a launcher"}


def first_pass(store: Any, worker: Worker) -> L.LearnResult:
    P.record_run(store, opening_run())
    worker.answers.append({"outcome": "failed", "cause": "the game never came up", "operations": [PIT], "used": []})
    res = L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    assert len(res) == 1 and res[0].mode == "postmortem"
    return res[0]


def test_the_users_own_fix_after_a_failed_opening_becomes_a_verified_way_and_launch_reads_it_back(store):
    worker = Worker(store)
    first = first_pass(store, worker)
    assert first.outcome == "failed" and P.live(store, ["procedure"]) == []
    timeline(store)
    # once its follow-up window has passed, the run is looked at again with what the user did
    worker.answers.append({"outcome": "failed", "cause": "the launcher's window is not the game",
                           "operations": [SHOWN], "used": []})
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert len(res) == 1 and res[0].mode == "postmortem" and any("stayed at" in r for r in res[0].reasons)
    sent = worker.calls[-1]["user"]
    assert "WHAT THE USER DID AFTER IT" in sent
    assert "Yuki's own effect" in sent.split("[u1]")[1].split("\n")[0]             # the opening's own window
    u4 = sent.split("[u4]")[1].split("\n")[0]
    assert "full screen" in u4 and "starfall-win64-shipping.exe <- starfall.exe <- fakelauncher.exe" in u4
    assert 'Start entries that run fakelauncher.exe: "Starfall" (--launch-product=starfall), "Fake Launcher"' in u4
    ways = P.live(store, ["procedure"])
    assert len(ways) == 1 and ways[0].origin == "demonstrated"
    assert ways[0].run == [{"do": "open_app", "name": "Starfall", "app": "starfall-win64-shipping"}]
    events = P.ledger(store, ways[0].id)
    assert [e.type for e in events if e.type in P.SUCCESS_EVENTS] == ["demonstrated"]
    assert P.learned_way("procedure", events) and P.provenance(store, ways[0])[0] == "user"
    assert ways[0].state == "candidate"                                            # one source until a run confirms it
    assert "demonstrated" in res[0].written and L.announces(res[0])
    # the next "open Starfall" finds it, and launch_app reads back the program it runs as
    assert [w["id"] for w in P.relevant(store, query_vec=vec("open starfall"))["ways"]] == [ways[0].id]
    assert P.launch_identities(store) == {"starfall": ["starfall-win64-shipping"]}
    assert MemoryClient(store).launch_identities() == {"starfall": ["starfall-win64-shipping"]}
    # looked at again once only
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 60) == []
    assert P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1")).signals[L.RECHECK_KEY]["result"].startswith(
        "postmortem")


@pytest.mark.parametrize("bad, why", [
    ({"entry": "Fake Launcher Pro"}, "names no Start entry"),
    ({"from_user": ["u1"]}, "Yuki's own effect"),
    ({"from_user": ["u2", "u9"]}, "not shown"),
    ({"from_user": ["u2", "u3"]}, "does not show it worked"),
])
def test_a_shown_way_is_checked_in_code_against_what_the_user_really_did(store, bad, why):
    worker = Worker(store)
    first_pass(store, worker)
    timeline(store)
    worker.answers.append({"outcome": "failed", "cause": "", "operations": [{**SHOWN, **bad}], "used": []})
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert P.live(store, ["procedure"]) == [] and any(why in r for r in res[0].rejected)


def test_a_run_that_worked_by_itself_learns_no_way_from_what_the_user_did_next(store):
    ctx = L.Ctx(mode="postmortem", verified=True, user_items={"u1": {"label": "u1", "worked": True, "yuki": False,
                                                                   "image": "starfall-win64-shipping.exe",
                                                                   "entries": [{"name": "Starfall"}]}})
    checked = L.check({"outcome": "worked", "operations": [{**SHOWN, "from_user": ["u1"]}], "used": []}, ctx)
    assert checked.ops and checked.ops[0]["demonstrated"]["entry"] == "Starfall"
    L.keep_ways_from_success(checked, ctx, success=True)
    assert checked.ops == [] and any("the run itself worked" in r for r in checked.rejected)


def test_nothing_new_after_the_judgement_costs_no_model_call(store):
    worker = Worker(store)
    first_pass(store, worker)                                     # no timeline rows of the user's own afterwards
    calls = len(worker.calls)
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls
    head = P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1"))
    assert head.signals[L.RECHECK_KEY]["result"] == "nothing new"


def test_an_opening_whose_game_window_showed_keeps_the_program_for_next_time() -> None:
    step = P.RunStep(step=2, at=NOW, tool="launch_app", kind="action", app="starfall-win64-shipping", target="",
                     input='{"name":"Starfall","watch":true}', ok=True, verdict=True, why="", result="", ms=1.0,
                     cost_usd=None, cited=[])
    assert P.executable_step(step) == {"do": "open_app", "name": "Starfall", "app": "starfall-win64-shipping"}
    plain = P.RunStep(step=1, at=NOW, tool="launch_app", kind="action", app="notesplus", target="",
                      input='{"name":"NotesPlus"}', ok=True, verdict=True, why="", result="", ms=1.0, cost_usd=None,
                      cited=[])
    assert P.executable_step(plain) == {"do": "open_app", "name": "NotesPlus"}   # the name is the program already
    assert P.clean_run([{"do": "open_app", "name": "Starfall", "app": "starfall-win64-shipping"}])[0]["app"]


def test_lineage_keeps_which_program_started_which_once_per_process() -> None:
    lineage.clear()
    record = lineage.note(os.getpid(), NOW)                       # this test's own process: read only
    assert record is not None and record["image"].startswith("python") and record["parents"]
    assert lineage.note(os.getpid(), NOW + 5) is None             # seen already: nothing more is read
    assert [r["image"] for r in lineage.between(NOW - 1)] == [record["image"]]


def test_memory_access_hands_launch_the_learned_programs_quickly_and_never_raises() -> None:
    from yuki.agent.memory import MemoryAccess

    class Access(MemoryAccess):
        def __init__(self, table: Any) -> None:
            self.table, self.asked = table, 0

        def call_method(self, name: str, *args: Any, timeout_s: float, **kwargs: Any) -> Any:
            self.asked += 1
            if isinstance(self.table, Exception):
                raise self.table
            return self.table

    access = Access({"starfall": ["starfall-win64-shipping"]})
    assert access.learned_launch_images("Starfall") == ["starfall-win64-shipping"]
    assert access.learned_launch_images("STARFALL") == ["starfall-win64-shipping"] and access.asked == 1  # cached
    assert Access(RuntimeError("memory is down")).learned_launch_images("Starfall") == []
