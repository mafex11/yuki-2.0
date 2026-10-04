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
#: The demonstration pass's answer (record_shown_way): the model's judgement that the user did the same thing.
DEMO = {"showed": True, "from_user": ["u2", "u3", "u4"], "entry": "Starfall", "intent": "open Starfall",
        "steps": ["Open the Start entry Starfall: Fake Launcher starts the game",
                  "Wait for the game's own window, full screen; Fake Launcher's windows are not the game"],
        "family": "opening a game that starts through a launcher", "why": "the user started the game themselves"}


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
    worker.answers.append(dict(DEMO))
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert len(res) == 1 and res[0].mode == "demonstration" and res[0].outcome == "shown"
    assert worker.calls[-1]["tool"]["name"] == "record_shown_way" and worker.calls[-1]["system"] == L.DEMO_PROMPT
    sent = worker.calls[-1]["user"]
    assert "WHAT THE USER DID AFTER IT" in sent
    assert "Yuki's own effect" in sent.split("[u1]")[1].split("\n")[0]             # the opening's own window
    u4 = sent.split("[u4]")[1].split("\n")[0]
    assert "full screen" in u4 and "starfall-win64-shipping.exe <- starfall.exe <- fakelauncher.exe" in u4
    assert 'Start entries that run fakelauncher.exe: "Starfall" (with --launch-product=starfall), "Fake Launcher"' in u4
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
    head = P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1"))
    assert head.signals[L.RECHECK_KEY]["result"] == "demonstration: shown"
    assert P.judged_ok(store, head.run_key) is False                               # Yuki's run did not work


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
    worker.answers.append({**DEMO, **bad})
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert P.live(store, ["procedure"]) == [] and any(why in r for r in res[0].rejected)
    assert res[0].outcome == "refused in code"


def test_when_the_model_judges_it_was_not_the_same_goal_nothing_is_written(store):
    worker = Worker(store)
    first_pass(store, worker)
    timeline(store)
    worker.answers.append({"showed": False, "why": "the user went on with other work"})
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert res[0].outcome == "not the same goal" and P.live(store, ["procedure"]) == []


def test_the_post_mortem_may_name_only_the_entry_and_the_listed_arguments_are_left_out(store):
    """Seen with the real model on the 5 Oct data: it named the entry with its arguments and no items."""
    ctx = L.Ctx(mode="postmortem", user_items={
        "u3": {"label": "u3", "worked": False, "yuki": False, "image": "", "entries": []},
        "u4": {"label": "u4", "worked": True, "yuki": False, "image": "starfall-win64-shipping.exe",
               "entries": [{"name": "Starfall", "arguments": "--launch-product=starfall"}, {"name": "Fake Launcher"}]}})
    raw = {**SHOWN, "from_user": [], "entry": "Starfall (with --launch-product=starfall)",
           "steps": [{"do": "Open the Start entry Starfall", "from": [9]}]}
    checked = L.check({"outcome": "failed", "operations": [raw], "used": []}, ctx)
    assert checked.ops and checked.ops[0]["demonstrated"] == {"items": ["u4"], "entry": "Starfall",
                                                              "image": "starfall-win64-shipping"}


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


# -- review fixes (5 Oct): Yuki's own slow launch is Yuki's, a verified opening is not doubted, the program belongs to
# -- the entry -------------------------------------------------------------------------------------------------------

SHELL = r"C:\Windows\SystemApps\Fake.Shell\searchhost.exe"


@pytest.fixture(autouse=True)
def shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell's program lives in the Windows folder; nothing real is read."""
    monkeypatch.setattr(L, "_running_exe", lambda key: SHELL if key == "searchhost" else "")
    L._shell_cache.clear()


def _game_from(store: Any, *, chain_at: float, window_at: float, start_row_at: float | None = None) -> None:
    """The game's window at ``window_at`` (full screen, 2 min), its launch chain under the launcher started at
    ``chain_at``; with ``start_row_at`` the user opened Start themselves then."""
    lineage.clear()
    lineage.add({"at": window_at, "image": "starfall-win64-shipping.exe", "exe": GAME, "started": chain_at + 5.0,
                 "parents": [{"image": "starfall.exe", "exe": BOOT, "started": chain_at},
                             {"image": "fakelauncher.exe", "exe": CARRIER, "started": NOW - 86400.0}],
                 "by_yuki": False})
    rows = [(window_at, 120.0, "starfall-win64-shipping", "Starfall", "STARFALL", True)]
    if start_row_at is not None:
        rows.insert(0, (start_row_at, 2.0, "searchhost", "Search", "Start", False))
    for start, active, process, app, title, full in rows:
        store.save_timeline(TimelineRow(id=None, started_at=start, ended_at=start + active, state="present",
                                        process=process, app=app, title=title, active_s=active, fullscreen=full))


def _voice_opening(*, handed: bool = True, verdict: bool | None = None) -> dict[str, Any]:
    run = opening_run(watched=verdict)
    if handed:
        run["steps"][0]["input"] = {"name": "Starfall", "handed_to": "fakelauncher.exe", "handed_at": NOW + 0.2}
    if verdict is None:
        run["steps"][1].update(ok=True, why="the watch stopped before it could tell",
                               result="stopped watching for Starfall's window after 30 s")
    return run


def _thinker_opening(*, handed: bool) -> dict[str, Any]:
    inp: dict[str, Any] = {"query": "Starfall"}
    if handed:
        inp.update(handed_to="fakelauncher.exe", handed_at=NOW + 0.2)
    steps = [{"tool": "launch_app", "kind": "action", "app": None, "target": "", "input": inp, "ok": True,
              "verdict": None, "why": "it is still starting through its launcher: no window of the app itself yet",
              "result": "started 'Starfall' the way the Start menu does: Fake Launcher took it; after 8 s it is "
                        "still starting", "ms": 8000.0, "at": NOW + 8.0}]
    return {"source": "agent", "session_id": "S5", "request_id": "voice-9", "lane": "voice", "at": NOW,
            "ended_at": NOW + 9.0, "words": WORDS, "reply": "It's starting.", "outcome": "final", "steps": steps,
            "wall_s": 9.0}


@pytest.mark.parametrize("make", [
    lambda: _voice_opening(handed=True),             # the voice watch was stopped (a second "open it")
    lambda: _thinker_opening(handed=True),           # the thinker: launch_app said "starting", no watch verdict
    lambda: _thinker_opening(handed=False),          # an opening kept without the hand-off facts: within 180 s
], ids=["voice-watch-stopped", "thinker-handed", "thinker-plain"])
def test_review_f1_a_game_yukis_own_launch_started_late_is_never_learned_as_the_users_fix(store, make):
    run = make()
    worker = Worker(store)
    P.record_run(store, run)
    end = float(run["ended_at"])
    worker.answers += [{"outcome": "failed", "cause": "not confirmed", "operations": [], "used": []}] * 2
    L.Learner(worker).run_once(now=end + L.FOLLOWUP_S + 1)
    calls = len(worker.calls)
    _game_from(store, chain_at=NOW + 3.0, window_at=NOW + 100.0)    # only the game, from Yuki's own launch
    head = P.run_header(store, P.run_key(run["source"], "S5", run["request_id"]))
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=end + L.CONTEXT_S + 5)
    assert len(items) == 1 and items[0]["yuki"] and not items[0]["worked"]
    assert "started by Yuki's own launch" in items[0]["yuki_why"]
    assert "started by Yuki's own launch" in L._user_item_line(items[0], lambda v, n: str(v)[:n])
    assert L.Learner(worker).run_once(now=end + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls                                # no demonstration pass, no post-mortem
    assert P.live(store, ["procedure"]) == []
    assert P.run_header(store, head.run_key).signals[L.RECHECK_KEY]["result"] == "nothing new"


def test_review_f1_the_user_taking_over_through_start_is_still_the_users_own_fix(store):
    worker = Worker(store)
    P.record_run(store, _voice_opening(handed=True, verdict=False))
    worker.answers.append({"outcome": "failed", "cause": "the game never came up", "operations": [PIT], "used": []})
    L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    _game_from(store, chain_at=NOW + 327.0, window_at=NOW + 330.0, start_row_at=NOW + 250.0)
    worker.answers.append({**DEMO, "from_user": ["u1", "u2"]})
    res = L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert len(res) == 1 and res[0].outcome == "shown"
    assert "Yuki's own effect" not in worker.calls[-1]["user"].split("[u2]")[1].split("\n")[0]


def test_review_f1_a_chain_yuki_started_before_the_user_went_to_start_stays_yukis(store):
    run = _voice_opening(handed=True, verdict=False)
    P.record_run(store, run)
    _game_from(store, chain_at=NOW + 3.0, window_at=NOW + 100.0, start_row_at=NOW + 30.0)
    head = P.run_header(store, P.run_key("hand", "S5", run["request_id"]))
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=END + L.CONTEXT_S)
    assert [it["yuki"] for it in items] == [False, True]             # Start is the user's; the game is Yuki's launch


def test_review_f3_a_verified_opening_is_not_doubted_and_never_takes_the_users_way(store):
    two = [P.RunStep(step=n, at=NOW, tool="launch_app", kind="action", app=None, target="",
                     input='{"name":"Starfall","handed_to":"fakelauncher.exe"}', ok=True, verdict=v, why="", result="",
                     ms=1.0, cost_usd=None, cited=[]) for n, v in ((1, None), (2, True))]
    assert not L.doubtful_open(two)                                  # the watch verified it
    assert L.doubtful_open(two[:1]) and L.doubtful_open([two[1], two[0]])
    worker = Worker(store, answers=[{"outcome": "worked", "operations": [], "used": []}] * 3)
    P.record_run(store, _voice_opening(handed=True, verdict=True))
    L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    calls = len(worker.calls)
    _game_from(store, chain_at=NOW + 327.0, window_at=NOW + 330.0, start_row_at=NOW + 250.0)
    L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5)
    assert len(worker.calls) == calls                                # no extra post-mortem for a verified opening
    # a pass that still cites the user's items on a verified run: the way is refused
    ctx = L.Ctx(mode="postmortem", verified=True, user_items={"u1": {"label": "u1", "worked": True, "yuki": False,
                                                                   "image": "starfall-win64-shipping.exe",
                                                                   "entries": [{"name": "Starfall"}]}})
    checked = L.check({"outcome": "failed", "operations": [{**SHOWN, "from_user": ["u1"]}], "used": []}, ctx)
    L.keep_ways_from_success(checked, ctx, success=False)
    assert not any(o.get("demonstrated") for o in checked.ops)
    assert any("the run itself worked" in r for r in checked.rejected)


@pytest.mark.parametrize("item, why", [
    ({"image": "moonfall-win64-shipping.exe", "exe": r"C:\Games\Moonfall\bin\moonfall-win64-shipping.exe"},
     "named for another Start entry ('Moonfall')"),
    ({"image": "fakelauncher.exe", "exe": CARRIER}, "hands its arguments to"),
])
def test_review_f4_the_program_learned_must_belong_to_the_entry(item, why):
    entries = [{"name": "Starfall", "arguments": "--launch-product=starfall", "program": "fakelauncher.exe"},
               {"name": "Moonfall", "arguments": "--launch-product=moonfall", "program": "fakelauncher.exe"},
               {"name": "Fake Launcher", "arguments": "", "program": "fakelauncher.exe"}]
    ctx = L.Ctx(mode="postmortem", user_items={"u4": {"label": "u4", "worked": True, "yuki": False, "entries": entries,
                                                      **item}})
    checked = L.check({"outcome": "failed", "operations": [{**SHOWN, "from_user": ["u4"]}], "used": []}, ctx)
    assert checked.ops == [] and any(why in r for r in checked.rejected)


# -- second review (5 Oct) --------------------------------------------------------------------------------------------


def _rows(store: Any, rows: list[tuple]) -> None:
    """``(start, active, passive, process, app, title, fullscreen)`` timeline rows."""
    for start, active, passive, process, app, title, full in rows:
        store.save_timeline(TimelineRow(id=None, started_at=start, ended_at=start + active + passive, state="present",
                                        process=process, app=app, title=title, active_s=active, passive_s=passive,
                                        fullscreen=full))


def _game_chain(*, window_at: float, chain_at: float) -> None:
    lineage.clear()
    lineage.add({"at": window_at, "image": "starfall-win64-shipping.exe", "exe": GAME, "started": chain_at + 1.0,
                 "parents": [{"image": "starfall.exe", "exe": BOOT, "started": chain_at},
                             {"image": "fakelauncher.exe", "exe": CARRIER, "started": NOW - 86400.0}],
                 "by_yuki": False})
    # the launcher's own UX process, running under it since long before the request
    lineage.add({"at": NOW + 200.0, "image": "fakelauncher ux.exe", "exe": CARRIER.replace("fakelauncher.exe",
                                                                                          r"uxakelauncher ux.exe"),
                 "started": NOW - 3600.0, "parents": [{"image": "fakelauncher.exe", "exe": CARRIER,
                                                        "started": NOW - 86400.0}], "by_yuki": False})


def _failed_voice_opening(store: Any, worker: Worker) -> P.RunHeader:
    P.record_run(store, _voice_opening(handed=True, verdict=False))
    worker.answers.append({"outcome": "failed", "cause": "the game never came up", "operations": [PIT], "used": []})
    L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    return P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1"))


@pytest.mark.parametrize("launcher_process", ["fakelauncher", "fakelauncher ux"], ids=["S3c", "S3d"])
def test_second_review_l1_a_launcher_update_that_delays_the_game_is_not_the_users_fix(store, launcher_process):
    """The launcher's window comes up by itself ("Updating 97%") long after the hand-off and the user only glances at
    it; the game Yuki's hand-off started then shows on its own. Yuki's, never a way the user showed."""
    worker = Worker(store)
    head = _failed_voice_opening(store, worker)
    calls = len(worker.calls)
    _rows(store, [(NOW + 200.0, 0.0, 80.0, launcher_process, "Fake Launcher", "Updating 97%", False),
                  (NOW + 300.0, 120.0, 0.0, "starfall-win64-shipping", "Starfall", "STARFALL", True)])
    _game_chain(window_at=NOW + 300.0, chain_at=NOW + 298.0)
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=END + L.CONTEXT_S + 5)
    game = [it for it in items if it["process"] == "starfall-win64-shipping"]
    assert game and game[0]["yuki"] and "started by Yuki's own launch" in game[0]["yuki_why"]
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls and P.live(store, ["procedure"]) == []


def test_second_review_l1_pressing_play_in_the_launchers_window_is_still_the_users_fix(store):
    worker = Worker(store)
    head = _failed_voice_opening(store, worker)
    _rows(store, [(NOW + 200.0, 12.0, 0.0, "fakelauncher ux", "Fake Launcher", "Fake Launcher", False),
                  (NOW + 300.0, 120.0, 0.0, "starfall-win64-shipping", "Starfall", "STARFALL", True)])
    _game_chain(window_at=NOW + 300.0, chain_at=NOW + 211.0)      # started as the user pressed Play
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=END + L.CONTEXT_S + 5)
    game = [it for it in items if it["process"] == "starfall-win64-shipping"]
    assert game and not game[0]["yuki"] and game[0]["worked"] and game[0]["fresh"]


def test_second_review_l2_a_way_the_first_post_mortem_learned_is_not_learned_again(store):
    """A quick user: the first post-mortem already saw what they did and wrote the way. The recheck neither writes it
    again (a second version, a second announcement) nor undoes the user's "No"."""
    worker = Worker(store)
    _game_from(store, chain_at=NOW + 238.0, window_at=NOW + 240.0, start_row_at=NOW + 190.0)
    P.record_run(store, _voice_opening(handed=True, verdict=False))
    worker.answers.append({"outcome": "failed", "cause": "the game never came up",
                           "operations": [{**SHOWN, "from_user": ["u1", "u2"]}], "used": []})
    first = L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    assert first and "demonstrated" in (first[0].written or {})
    ways = P.live(store, ["procedure"])
    assert len(ways) == 1
    assert P.workflow_edit(store, f"pb:{ways[0].id}", "forget", now=END + 200.0)["ok"]   # the user said No
    calls = len(worker.calls)
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls
    head = P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1"))
    assert head.signals[L.RECHECK_KEY]["result"] == "already learned what the user showed"
    assert len(store._query("SELECT id FROM playbooks WHERE kind='procedure'")) == 1     # no second version
    assert P.live(store, ["procedure"]) == []                            # the No stands


def test_second_review_l3_switching_to_a_game_already_running_is_not_a_way_shown(store):
    """The service restarted while the game ran (started 2 h earlier); Yuki could not bring it forward and the user
    alt-tabbed to it. Its program started before the request: no demonstration pass, and none could be written."""
    worker = Worker(store)
    run = _voice_opening(handed=False, verdict=False)
    run["steps"] = run["steps"][:1]
    run["steps"][0].update(verdict=False, ok=True, why="its window would not come to the front")
    P.record_run(store, run)
    worker.answers.append({"outcome": "failed", "cause": "focus refused", "operations": [], "used": []})
    L.Learner(worker).run_once(now=END + L.FOLLOWUP_S + 1)
    calls = len(worker.calls)
    _rows(store, [(NOW + 20.0, 400.0, 0.0, "starfall-win64-shipping", "Starfall", "STARFALL", True)])
    _game_chain(window_at=NOW + 20.0, chain_at=NOW - 7200.0)
    head = P.run_header(store, P.run_key("hand", "S5", "voice-s1-3-h1"))
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=END + L.CONTEXT_S + 5)
    assert items[0]["image"] and items[0]["entries"] and items[0]["fresh"] is False
    assert not L.demonstration_item(items[0])
    assert "already running before the request" in L._user_item_line(items[0], lambda v, n: str(v)[:n])
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls
    ctx = L.Ctx(mode="postmortem", user_items={it["label"]: it for it in items})
    checked = L.check({"outcome": "failed", "operations": [{**SHOWN, "from_user": ["u1"]}], "used": []}, ctx)
    assert checked.ops == [] and any("already running before the request" in r for r in checked.rejected)


def test_second_review_l4_l5_unknown_starts_and_the_users_other_windows_cost_no_recheck(store):
    """Lineage lost (service restart): the game Yuki's own slow launch started shows with no launch facts - who
    started it is unknown, never the user's doing. The user reading the browser meanwhile is no evidence either."""
    worker = Worker(store)
    head = _failed_voice_opening(store, worker)
    calls = len(worker.calls)
    lineage.clear()
    _rows(store, [(NOW + 200.0, 190.0, 0.0, "chrome", "Google Chrome", "Some article", False),
                  (NOW + 400.0, 120.0, 0.0, "starfall-win64-shipping", "Starfall", "STARFALL", True)])
    items = L.user_next(store, head, P.run_steps(store, head.run_key), now=END + L.CONTEXT_S + 5)
    game = [it for it in items if it["process"] == "starfall-win64-shipping"][0]
    browser = [it for it in items if it["process"] == "chrome"][0]
    assert game["unknown"] and not game["worked"] and browser["worked"] and not browser["unknown"]
    assert "who started it is not known" in L._user_item_line(game, lambda v, n: str(v)[:n])
    assert L.Learner(worker).run_once(now=END + L.CONTEXT_S + 5) == []
    assert len(worker.calls) == calls                                    # no recheck post-mortem
    assert P.run_header(store, head.run_key).signals[L.RECHECK_KEY]["result"] == "nothing new"


def test_second_review_l6_the_watch_step_is_not_a_second_open() -> None:
    def step(n: int, inp: str, verdict: Any, app: Any = None) -> P.RunStep:
        return P.RunStep(step=n, at=NOW + n, tool="launch_app", kind="action", app=app, target="", input=inp, ok=True,
                         verdict=verdict, why="", result="", ms=1.0, cost_usd=None, cited=[])

    ctx = L.Ctx(mode="distill", steps={1: step(1, '{"name":"Starfall"}', None),
                                       2: step(2, '{"name":"Starfall","watch":true}', True, "starfall-win64-shipping")})
    run = L.executable_run([{"do": "Open Starfall", "from": [1, 2]}], ctx, [])
    assert run == [{"do": "open_app", "name": "Starfall", "app": "starfall-win64-shipping"}]
    run = L.executable_run([{"do": "Open Starfall", "from": [1]}, {"do": "Wait for it", "from": [2]}], ctx, [])
    assert run == [{"do": "open_app", "name": "Starfall", "app": "starfall-win64-shipping"}]


def test_second_review_v3_a_plain_opening_that_could_not_be_confirmed_is_not_doubtful() -> None:
    def step(inp: str, verdict: Any) -> P.RunStep:
        return P.RunStep(step=1, at=NOW, tool="launch_app", kind="action", app=None, target="", input=inp, ok=True,
                         verdict=verdict, why="", result="", ms=1.0, cost_usd=None, cited=[])

    assert not L.doubtful_open([step('{"name":"File Explorer"}', None)])
    assert L.doubtful_open([step('{"name":"File Explorer"}', False)])
    assert L.doubtful_open([step('{"name":"Starfall","handed_to":"fakelauncher.exe"}', None)])
