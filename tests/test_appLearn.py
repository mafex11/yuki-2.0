"""appLearn (4 Oct, the design notes): only a verified success is learned and
announced. The user: "the things it should learn should be the ones that it successfully executed. It just gives me the
card that it learned this and that, but it was all failure."

A failed or stopped run: no Learned card, no visible learned row, no procedure (its pitfall stays a quiet note for the
brain). A verified success: one Learned card and a Playbook procedure. A failure then a success of the same request:
one card, for the success, carrying what did not work.

Offline: temp databases, a word-bag stand-in for the embedder, stand-ins for the model calls; nothing touches the live
desktop, the real memory or the network. Names are invented."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from test_c_practice import NOW, Worker, step, store, vec  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory.api import MemoryClient

WORDS = "close the lyrics view in spotify"


def lyrics_run(request_id: str = "worker-1", *, at: float = NOW, outcome: str = "cancelled",
               steps: list[dict[str, Any]] | None = None, words: str = WORDS) -> dict[str, Any]:
    """The 3 Oct Spotify job, invented details: focus the player, then the Lyrics toggle clicked again and again with no
    effect, and the user stopped it."""
    steps = steps if steps is not None else [
        step("focus_window", app="spotify", target='window "Spotify"', result="Spotify is in front"),
        *[step("click", app="spotify", verdict=False, target='Button "Lyrics"', result="the lyrics view did not change",
               sig="click:lyrics") for _ in range(3)],
    ]
    return {"source": "agent", "session_id": "S9", "request_id": request_id, "lane": "voice", "at": at,
            "ended_at": at + 30, "words": words, "reply": "Stopped." if outcome != "final" else "Closed it.",
            "outcome": outcome, "steps": steps, "cost_usd": 0.05, "model_calls": 4, "wall_s": 30.0}


FAILED_PIT = {"op": "ADD", "kind": "pitfall", "intent": "close the lyrics view in Spotify", "apps": ["spotify"],
              "text": "clicking the Lyrics toggle again and again did not close the lyrics view", "from": [2, 3, 4],
              "instead": "ask the user first", "family": "toggles in a music player"}
#: What a post-mortem may still try to keep from a failed run: refused in code (the run did not succeed).
FAILED_PROC = {"op": "ADD", "kind": "procedure", "intent": "close the lyrics view in Spotify", "apps": ["spotify"],
               "steps": [{"do": "bring Spotify to the front", "from": [1]}]}
SUCCESS_PROC = {"op": "ADD", "kind": "procedure", "intent": "close the lyrics view in Spotify", "apps": ["spotify"],
                "steps": [{"do": "click the Close lyrics button", "from": [1]}],
                "avoid": "clicking the Lyrics toggle again and again"}


def page_text(page: Any) -> str:
    return json.dumps(page, ensure_ascii=False, default=str)


def learn_failed(store: Any, worker: Worker) -> L.LearnResult:
    P.record_run(store, lyrics_run())
    worker.answers.append({"outcome": "cancelled", "cause": "the toggle did not change the view",
                           "operations": [FAILED_PIT, FAILED_PROC], "used": []})
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert len(res) == 1
    return res[0]


# -- a failed or stopped run -------------------------------------------------------------------------------------------


def test_a_failed_run_makes_no_procedure_no_learned_card_and_no_visible_row(store):
    from yuki.ui.app import learned_for_card
    from yuki.ui.web_data import build_playbook

    worker = Worker(store)
    res = learn_failed(store, worker)
    assert res.mode == "postmortem" and res.succeeded is False
    assert P.live(store, ["procedure", "habit"]) == []                          # the failed method is never a way
    assert any("verified success" in r for r in res.rejected)
    # what failed is kept, quietly: the brain sees it for that app, so the toggle is not clicked six times again
    pits = P.live(store, ["pitfall"])
    assert len(pits) == 1
    shown = P.relevant(store, query_vec=vec(WORDS), apps_in_play=["Spotify.exe"])
    assert [p["id"] for p in shown["pitfalls"]] == [pits[0].id] and shown["ways"] == []
    # ...and never shown or announced as learning
    page = P.workflows(store)
    assert page["procedures"] == page["lessons"] == page["proposals"] == page["learned"] == []
    assert learned_for_card(page, NOW - 1) is None
    view = build_playbook(MemoryClient(store))
    assert view["procedures"] == view["lessons"] == view["proposals"] == []
    assert "Lyrics toggle" not in page_text(view)


def test_the_agents_own_session_pitfall_from_a_stopped_run_is_never_announced(store):
    """The card the user saw on 3 Oct: the agent wrote the stopped run's pitfall as the run ended (origin "session"),
    and the return card announced it as "Learned". It stays a quiet note."""
    from yuki.ui.app import learned_for_card

    P.record_run(store, lyrics_run())
    key = P.run_key("agent", "S9", "worker-1")
    pid = P.add_playbook(store, {"kind": "pitfall", "intent": WORDS, "apps": ["spotify"], "origin": "session",
                                 "body": {"text": "click on Button \"Lyrics\", 3 times: it did not change"}},
                         events=[{"type": "observed", "run_key": key, "refs": {"outcome": "cancelled"}}],
                         vector=vec(WORDS), now=NOW + 31)
    page = P.workflows(store, now=NOW + 32)
    assert f"pb:{pid}" in {c["id"] for c in page["workflows"]}                  # kept (chip titles only)
    assert page["proposals"] == page["learned"] == page["lessons"] == []
    assert learned_for_card(page, NOW) is None


class SyncThread:
    """``threading.Thread`` that runs its target at ``start()`` (the return card's memory read, made synchronous)."""

    def __init__(self, target: Any = None, args: tuple = (), name: str = "", daemon: bool = False) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        self.target(*self.args)


class FakeWaiter:
    """:class:`yuki.ui.learned_wait.LearnedWaiter` with its waits recorded: the test plays the learner's signal by
    calling the armed check."""

    def __init__(self) -> None:
        self.armed: list[tuple[Any, float]] = []
        self.cancelled = 0

    def prepare(self) -> bool:
        return True

    def arm(self, check: Any, within_s: float) -> bool:
        self.armed.append((check, within_s))
        return True

    def cancel(self) -> None:
        self.cancelled += 1

    def stop(self) -> None:
        pass


class Timers:
    """``QTimer`` with ``singleShot`` recorded: run now when ``run_now``, else kept (the late read is checked apart)."""

    def __init__(self) -> None:
        self.kept: list[tuple[int, Any]] = []

    def singleShot(self, ms: int, fn: Any) -> None:   # noqa: N802 - Qt's name
        if ms >= 60_000:
            self.kept.append((ms, fn))
        else:
            fn()


@pytest.fixture
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _ui(qapp: Any, monkeypatch: Any, page: Any) -> Any:
    from test_appC1_run_text import _bare_ui
    from yuki.ui import app as app_mod

    ui = _bare_ui(qapp, voice_owns=False)
    reads: list[str] = []

    def call_method(name: str, *a: Any, **k: Any) -> Any:
        reads.append(name)
        return page() if callable(page) else page

    ui.runtime = SimpleNamespace(memory=SimpleNamespace(call_method=call_method))
    ui._returns = app_mod._Bridge()
    ui._returns.done.connect(ui._on_return_ready)
    timers = Timers()
    monkeypatch.setattr(app_mod, "QTimer", timers)
    monkeypatch.setattr(threading, "Thread", SyncThread)
    ui.reads, ui.timers = reads, timers
    ui._learned_waiter = ui.waiter = FakeWaiter()
    return ui


CLOSED = [{"name": "run_powershell", "ok": True, "input": {"what": "close the lyrics", "_about": {"changes": True}}}]


def test_a_stopped_runs_return_card_has_no_learned_box_and_reads_nothing(qapp, monkeypatch, store):
    worker = Worker(store)
    learn_failed(store, worker)
    ui = _ui(qapp, monkeypatch, lambda: P.workflows(store))
    ui._queue_return_card(4, "Stopped.", "stopped", {"started": 0.0, "calls": CLOSED})
    card = ui.return_card
    assert card is not None and card.learn.isHidden() and ui.island.lesson_available is False
    assert ui.reads == [] and ui.timers.kept == [] and ui.waiter.armed == []   # no memory read, no wait
    card.close()


# -- a verified success ----------------------------------------------------------------------------------------------


def playlist_run(request_id: str = "worker-5", *, at: float = NOW, last_verdict: bool | None = True) -> dict[str, Any]:
    steps = [step("launch_app", app="spotify", inp={"query": "spotify"}, result="Spotify opened"),
             step("click", app="spotify", target='Edit "What do you want to play?"', result="the search box is focused"),
             step("type_text", app="spotify", inp={"text": "focus mix", "press_enter": True},
                  target='Edit "What do you want to play?"', result="typed; results shown"),
             step("click", app="spotify", verdict=last_verdict, target='Button "Play Focus mix"',
                  result="the playlist is playing")]
    return {"source": "agent", "session_id": "S7", "request_id": request_id, "lane": "voice", "at": at,
            "ended_at": at + 20, "words": "play my focus mix in spotify", "reply": "Playing it.", "outcome": "final",
            "steps": steps, "cost_usd": 0.08, "model_calls": 5, "wall_s": 20.0}


PLAYLIST_PROC = {"op": "ADD", "kind": "procedure", "intent": "play one of your playlists in Spotify",
                 "apps": ["spotify"], "slots": ["playlist"],
                 "steps": [{"do": "open Spotify", "from": [1]}, {"do": "search for the playlist", "from": [2, 3]},
                           {"do": "click its Play button", "from": [4]}]}


def test_a_verified_success_makes_one_learned_card_and_a_playbook_procedure(qapp, monkeypatch, store):
    from yuki.ui.app import learned_for_card
    from yuki.ui.web_data import build_playbook

    P.record_run(store, playlist_run())
    worker = Worker(store, [{"outcome": "worked", "operations": [PLAYLIST_PROC], "used": []}])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].mode == "distill" and res[0].succeeded is True
    procs = P.live(store, ["procedure"])
    assert len(procs) == 1 and "succeeded" in [e.type for e in P.ledger(store, procs[0].id)]
    page = P.workflows(store, now=NOW + 201)
    assert [c["id"] for c in page["learned"]] == [procs[0].ref]
    assert procs[0].ref in {c["id"] for c in page["proposals"] + page["procedures"]}
    assert build_playbook(MemoryClient(store))["proposals"][0]["ref"] == procs[0].ref   # the Playbook's NEW block
    card = learned_for_card(page, NOW)
    assert card == {"id": procs[0].ref, "text": "Play one of your playlists in Spotify", "why": ""}
    # on the return card: a done run whose success taught it shows exactly this one Learned box
    ui = _ui(qapp, monkeypatch, page)
    monkeypatch.setattr("time.time", lambda: NOW + 22)   # the run started at NOW and took 22 s
    ui._queue_return_card(5, "Playing it.", "done", {"started": __import__("time").monotonic() - 22, "calls": CLOSED})
    assert ui.reads == ["workflows"] and ui.waiter.armed == []                  # found at once: no wait
    assert not ui.return_card.learn.isHidden() and ui.return_card.learn.ref == procs[0].ref
    assert ui.island.lesson_available is True
    ui.return_card.close()


def test_a_run_that_ended_done_but_its_last_step_was_not_verified_teaches_no_way(store):
    P.record_run(store, playlist_run(last_verdict=False))
    worker = Worker(store, [{"outcome": "worked", "operations": [dict(PLAYLIST_PROC, steps=PLAYLIST_PROC["steps"][:2])],
                             "used": []}])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].succeeded is False and P.live(store, ["procedure"]) == []
    assert P.workflows(store)["learned"] == []


def test_a_done_run_learned_later_gets_its_learned_box_when_the_learner_signals(qapp, monkeypatch, store):
    """The learner writes a run's way after its follow-up window (other runs' passes may come first): the run's card
    waits for the learner's signal, reads once per signal, and shows the box when its success taught a way."""
    from yuki.ui import app as app_mod

    pages = [{"available": True, "learned": []}]
    ui = _ui(qapp, monkeypatch, lambda: pages[-1])
    monkeypatch.setattr("time.time", lambda: NOW + 30)
    ui._queue_return_card(6, "Playing it.", "done", {"started": __import__("time").monotonic() - 20, "calls": CLOSED})
    assert ui.return_card.learn.isHidden() and ui.timers.kept == [] and len(ui.waiter.armed) == 1
    late, within = ui.waiter.armed[0]
    assert within == app_mod.LEARNED_WAIT_S
    assert late() is False and ui.return_card.learn.isHidden()                 # a signal for another run: wait on
    pages.append({"available": True, "learned": [{"id": "pb:9", "kind": "procedure", "title": "play one of your "
                  "playlists in Spotify", "learned_at": NOW + 140, "why": ""}]})
    assert late() is True                                                       # this run's success: the wait ends
    assert not ui.return_card.learn.isHidden() and ui.return_card.learn.ref == "pb:9"
    assert ui.island.lesson_available is True and ui._return_learned["id"] == "pb:9"
    # once a later run has ended, an earlier run's late learning never lands on a card
    ui._return_last_id = 7
    ui._on_return_ready({"late": True, "id": 6, "learned": {"id": "pb:10", "text": "x"}, "view": {}, "took": ""})
    assert ui.return_card.learn.ref == "pb:9"
    ui.return_card.close()


CLICK_ONLY = [{"name": "click", "ok": True, "input": {"_about": {"element": "Close lyrics"}}}]


def test_a_done_run_with_nothing_else_to_tell_gets_a_card_only_for_what_it_learned(qapp, monkeypatch, store):
    """One click that worked is not worth a return card by itself (the strip said it); a way its success taught is."""
    pages = [{"available": True, "learned": []}]
    ui = _ui(qapp, monkeypatch, lambda: pages[-1])
    monkeypatch.setattr("time.time", lambda: NOW + 10)
    ui._queue_return_card(8, "Closed it.", "done", {"started": __import__("time").monotonic() - 10,
                                                   "calls": CLICK_ONLY})
    assert ui.return_card is None and len(ui.waiter.armed) == 1                 # no card yet, the wait armed
    pages.append({"available": True, "learned": [{"id": "pb:11", "kind": "procedure", "run_at": NOW,
                  "title": "close the lyrics view in Spotify", "learned_at": NOW + 150,
                  "why": "clicking the Lyrics toggle again and again"}]})
    assert ui.waiter.armed[0][0]() is True
    card = ui.return_card
    assert card is not None and not card.learn.isHidden() and card.learn.ref == "pb:11"
    assert card.learn.why.text() == "What didn’t work: clicking the Lyrics toggle again and again"
    card.close()
    # a stopped run with nothing to tell: no card, no read, nothing armed
    ui2 = _ui(qapp, monkeypatch, lambda: pages[-1])
    ui2._queue_return_card(9, "Stopped.", "stopped", {"started": 0.0, "calls": CLICK_ONLY})
    assert ui2.return_card is None and ui2.reads == [] and ui2.waiter.armed == []


# -- a failure, then the same request succeeded ------------------------------------------------------------------------


def test_failure_then_success_one_card_for_the_success_with_what_did_not_work(store):
    from yuki.ui.app import learned_for_card
    from yuki.ui.web_data import build_playbook

    worker = Worker(store)
    learn_failed(store, worker)
    assert P.workflows(store)["learned"] == []
    # the same request, 10 minutes later: one click on the right button, verified. One action alone would teach
    # nothing; after the failed try, its success is what is learned
    ok = [step("click", app="spotify", target='Button "Close lyrics"', result="the lyrics view closed")]
    P.record_run(store, lyrics_run("worker-2", at=NOW + 600, outcome="final", steps=ok))
    worker.answers.append({"outcome": "worked", "operations": [SUCCESS_PROC], "used": []})
    res = L.Learner(worker).run_once(now=NOW + 800)
    assert res[0].mode == "distill" and res[0].succeeded is True
    assert any("earlier try of this request" in r for r in res[0].reasons)
    assert "EXISTING PLAYBOOKS" in worker.calls[-1]["user"] and "Lyrics toggle" in worker.calls[-1]["user"]
    page = P.workflows(store, now=NOW + 801)
    assert len(page["learned"]) == 1
    card = learned_for_card(page, NOW + 600, NOW + 630)
    assert learned_for_card(page, NOW, NOW + 30) is None                        # never on the failed run's card
    assert card["text"] == "Close the lyrics view in Spotify"
    assert card["why"] == "What didn’t work: clicking the Lyrics toggle again and again"
    view = build_playbook(MemoryClient(store))
    assert [p["ref"] for p in view["proposals"]] == [card["id"]] and view["lessons"] == []
    # the failed method itself is still nowhere on the page: only inside the success, as what did not work
    assert "did not close the lyrics view" not in page_text(view)


# -- the agent's own code-learned way (the slider), marked by the run's judgement ---------------------------------------


def slider_run(request_id: str, *, at: float) -> dict[str, Any]:
    return {"source": "agent", "session_id": "S5", "request_id": request_id, "lane": "voice", "at": at,
            "ended_at": at + 6, "words": "turn the volume up inside spotify", "reply": "It's at 80% now.",
            "outcome": "final", "model_calls": 2, "wall_s": 6.0,
            "steps": [step("set_slider", app="spotify", target='Text "Change volume"', result="set to 80, read back")]}


def slider_way(store: Any, key: str, at: float) -> int:
    return P.add_playbook(store, {"kind": "procedure", "intent": "turn the volume up inside spotify", "apps": ["spotify"],
                                  "origin": "slider", "body": {"steps": ["Read the window", "set_slider on it"]}},
                          events=[{"type": "observed", "run_key": key, "refs": {"outcome": "final"}, "at": at}],
                          vector=vec("turn the volume up inside spotify"), now=at)


def test_the_agents_slider_way_is_announced_once_the_learner_finds_the_run_a_success(store):
    P.record_run(store, slider_run("worker-1", at=NOW))
    key = P.run_key("agent", "S5", "worker-1")
    pid = slider_way(store, key, NOW + 6)
    # before the learner read the run: offered to the brain (its trace is a verified success), not announced yet
    got = P.relevant(store, query_vec=vec("turn the volume up inside spotify"), apps_in_play=["spotify"])
    assert [w["id"] for w in got["ways"]] == [pid] and P.workflows(store)["learned"] == []
    res = L.Learner(Worker(store)).run_once(now=NOW + 200)
    assert res[0].skipped == "no trigger" and res[0].stamped == [(pid, key)]
    assert [c["id"] for c in P.workflows(store)["learned"]] == [f"pb:{pid}"]


def test_a_way_from_a_run_the_user_corrected_is_never_announced_or_offered(store):
    P.record_run(store, slider_run("worker-1", at=NOW))
    key = P.run_key("agent", "S5", "worker-1")
    pid = slider_way(store, key, NOW + 6)
    store.add_turn("S5", "worker-1", NOW, "turn the volume up inside spotify", "It's at 80% now.", ["set_slider"],
                   "final")
    store.add_turn("S5", "worker-2", NOW + 15, "no, the other slider, the song's own", "Okay.", [], "final")
    worker = Worker(store, [{"outcome": "corrected", "cause": "the wrong slider", "operations": [], "used": []}])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].mode == "postmortem" and res[0].succeeded is False and res[0].stamped == []
    page = P.workflows(store, now=NOW + 201)
    assert page["learned"] == [] and f"pb:{pid}" not in {c["id"] for c in page["proposals"] + page["procedures"]}
    got = P.relevant(store, query_vec=vec("turn the volume up inside spotify"), apps_in_play=["spotify"])
    assert got["ways"] == []


# -- the backfill (ways learned before appLearn) ---------------------------------------------------------------------


def test_the_backfill_marks_ways_from_verified_runs_and_its_dry_run_writes_nothing(store):
    P.record_run(store, playlist_run())
    good_key = P.run_key("agent", "S7", "worker-5")
    P.set_learned(store, good_key, "done", "distill: worked")
    P.record_run(store, lyrics_run())
    bad_key = P.run_key("agent", "S9", "worker-1")
    P.set_learned(store, bad_key, "done", "postmortem: cancelled")
    good = P.add_playbook(store, {"kind": "procedure", "intent": "play a playlist", "origin": "distill",
                                  "body": {"steps": ["search", "play"]}}, events=[{"type": "observed",
                                                                                  "run_key": good_key}])
    bad = P.add_playbook(store, {"kind": "procedure", "intent": "close the lyrics", "origin": "postmortem",
                                 "body": {"steps": ["focus"]}}, events=[{"type": "observed", "run_key": bad_key}])
    assert P.stamp_successes(store, dry_run=True) == [(good, good_key)]
    assert "succeeded" not in [e.type for e in P.ledger(store, good)]          # a dry run writes nothing
    assert P.stamp_successes(store) == [(good, good_key)]
    assert "succeeded" in [e.type for e in P.ledger(store, good)]
    assert P.stamp_successes(store) == []                                       # once
    page = P.workflows(store)
    assert [c["id"] for c in page["learned"]] == [f"pb:{good}"]
    # dated by the run's own end: an old way marked today is never "just learned" on a running job's card
    assert page["learned"][0]["learned_at"] == NOW + 20
    assert f"pb:{bad}" not in page_text({k: page[k] for k in ("procedures", "proposals", "learned")})
    assert P.learned_way("procedure", P.ledger(store, bad)) is False


# -- review fixes (F1): the learner's full verdict, not only the model's outcome word ---------------------------------


CORRECTION = "no, the other slider, the song's own"
SLIDER_PIT = {"op": "ADD", "kind": "pitfall", "intent": "turn the volume up inside spotify", "apps": ["spotify"],
              "text": "the window's volume slider is not the song's own", "from": [1], "quote": CORRECTION,
              "instead": "use the song's own slider"}


def _corrected_slider(store: Any) -> tuple[str, int]:
    P.record_run(store, slider_run("worker-1", at=NOW))
    key = P.run_key("agent", "S5", "worker-1")
    pid = slider_way(store, key, NOW + 6)
    store.add_turn("S5", "worker-1", NOW, "turn the volume up inside spotify", "It's at 80% now.", ["set_slider"],
                   "final")
    store.add_turn("S5", "worker-2", NOW + 15, CORRECTION, "Okay.", [], "final")
    return key, pid


def _never_a_way(store: Any, key: str, pid: int) -> None:
    assert P.judged_ok(store, key) is False and P.corrected_by_quote(store, key) is True
    got = P.relevant(store, query_vec=vec("turn the volume up inside spotify"), apps_in_play=["spotify"])
    assert pid not in [w["id"] for w in got["ways"]]
    page = P.workflows(store, now=NOW + 201)
    assert page["learned"] == [] and f"pb:{pid}" not in {c["id"] for c in page["proposals"] + page["procedures"]}
    assert P.stamp_successes(store) == []                                       # the daily backfill marks nothing
    assert P.workflows(store, now=NOW + 202)["learned"] == []


def test_a_run_corrected_by_quote_is_never_a_way_even_when_the_pass_labelled_it_worked(store):
    """The model's outcome word varies: a post-mortem labelled worked_after_trouble whose pitfall quotes the user's
    correction is a corrected run. The learner notes its own verdict, so nothing reads the run as a success later."""
    key, pid = _corrected_slider(store)
    worker = Worker(store, [{"outcome": "worked_after_trouble", "cause": "the wrong slider at first",
                             "operations": [SLIDER_PIT], "used": []}])
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].mode == "postmortem" and res[0].succeeded is False and res[0].stamped == []
    note = store._query("SELECT learn_note FROM runs WHERE run_key=? AND step=0", (key,))[0]["learn_note"]
    assert note == "postmortem: corrected"
    _never_a_way(store, key, pid)


def test_a_run_judged_before_the_fix_whose_lesson_quotes_the_correction_is_not_backfilled(store):
    """A run the older learner noted only by the model's word ("postmortem: worked_after_trouble") whose pitfall quotes
    the user's correction: read back from the ledger, still a corrected run."""
    key, pid = _corrected_slider(store)
    P.set_learned(store, key, "done", "postmortem: worked_after_trouble")
    P.add_playbook(store, {"kind": "pitfall", "intent": "turn the volume up inside spotify", "apps": ["spotify"],
                           "origin": "postmortem", "body": {"text": SLIDER_PIT["text"]}},
                   events=[{"type": "observed", "run_key": key}, {"type": "taught", "run_key": key,
                                                                  "refs": {"quote": CORRECTION}}],
                   vector=vec("the volume slider in spotify"), now=NOW + 200)
    _never_a_way(store, key, pid)


def test_a_quote_from_the_users_answer_to_a_question_is_not_a_correction(store):
    """Only the user's next or added words count: a lesson quoting something else leaves a worked run a success."""
    P.record_run(store, slider_run("worker-1", at=NOW))
    key = P.run_key("agent", "S5", "worker-1")
    pid = slider_way(store, key, NOW + 6)
    P.set_learned(store, key, "done", "postmortem: worked_after_trouble")
    P.add_playbook(store, {"kind": "pitfall", "intent": "turn the volume up inside spotify", "apps": ["spotify"],
                           "origin": "postmortem", "body": {"text": "ask which slider first"}},
                   events=[{"type": "observed", "run_key": key}, {"type": "taught", "run_key": key,
                                                                  "refs": {"quote": "the song's own"}}],
                   vector=vec("the volume slider in spotify"), now=NOW + 200)
    assert P.corrected_by_quote(store, key) is False and P.judged_ok(store, key) is True
    assert P.stamp_successes(store) == [(pid, key)]


# -- review fixes (F2): the late Learned box comes on the learner's signal --------------------------------------------


def test_the_learner_signals_only_for_a_run_that_taught_or_marked_a_way(store, monkeypatch):
    signals: list[int] = []
    monkeypatch.setattr(P, "signal_learned", lambda: signals.append(1) or True)
    worker = Worker(store)
    learn_failed(store, worker)
    assert signals == []                                                        # a failed run: nothing to announce
    P.record_run(store, playlist_run())
    worker.answers.append({"outcome": "worked", "operations": [PLAYLIST_PROC], "used": []})
    res = L.Learner(worker).run_once(now=NOW + 200)
    assert res[0].succeeded is True and signals == [1]


def test_the_tray_and_memory_name_the_same_event():
    from yuki.ui.learned_wait import LEARNED_EVENT_NAME

    assert LEARNED_EVENT_NAME == P.LEARNED_EVENT_NAME


def _wait_until(cond: Any, timeout_s: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_the_waiter_reads_once_per_signal_until_the_run_is_found():
    """A real named event (a name of the test's own; nothing else waits on it), set as memory sets it."""
    import uuid

    from yuki.memory.store import signal_event
    from yuki.ui.learned_wait import LearnedWaiter

    name = r"Local\YukiLearnedTest-" + uuid.uuid4().hex
    waiter = LearnedWaiter(name)
    answers = [False, True]
    calls: list[int] = []

    def check() -> bool:
        calls.append(1)
        return answers[len(calls) - 1]

    try:
        assert waiter.arm(check, 30.0) and waiter.waiting
        assert signal_event(name) and _wait_until(lambda: len(calls) == 1)
        assert waiter.waiting                                                   # not this run's yet: wait on
        assert signal_event(name) and _wait_until(lambda: not waiter.waiting)
        assert len(calls) == 2
        # at the deadline, one last read (a signal may have come before the event existed)
        late: list[int] = []
        assert waiter.arm(lambda: late.append(1) or False, 0.2)
        assert _wait_until(lambda: not waiter.waiting) and late == [1]
        # a newer run's wait ends the one before without reading for it
        first: list[int] = []
        assert waiter.arm(lambda: first.append(1) or False, 30.0)
        second: list[int] = []
        assert waiter.arm(lambda: second.append(1) or True, 30.0)
        assert signal_event(name) and _wait_until(lambda: second == [1])
        assert first == [] and _wait_until(lambda: not waiter.waiting)
    finally:
        waiter.stop()


# -- review fixes (F3, F4): the late card's Save key and time ---------------------------------------------------------


def test_a_quick_question_after_the_run_never_offers_its_lesson_and_the_late_card_keeps_the_runs_time(qapp, monkeypatch,
                                                                                                    store):
    from datetime import datetime

    from yuki.ui import app as app_mod

    stamps: list[datetime] = []
    real_clock = app_mod.clock
    monkeypatch.setattr(app_mod, "clock", lambda d: stamps.append(d) or real_clock(d))
    pages = [{"available": True, "learned": []}]
    ui = _ui(qapp, monkeypatch, lambda: pages[-1])
    monkeypatch.setattr("time.time", lambda: NOW + 10)                         # the run ended at NOW + 10
    ui._queue_return_card(12, "Closed it.", "done", {"started": __import__("time").monotonic() - 10,
                                                    "calls": CLICK_ONLY})
    assert ui.return_card is None and len(ui.waiter.armed) == 1
    # a quick question, no actions (no return card of its own), started and answered
    ui._on_started("front_desk", 13, "what time is it")
    ui._runs.pop(13, None)
    pages.append({"available": True, "learned": [{"id": "pb:12", "kind": "procedure", "run_at": NOW,
                  "title": "close the lyrics view in Spotify", "learned_at": NOW + 150, "why": ""}]})
    monkeypatch.setattr("time.time", lambda: NOW + 160)                        # the learner's signal, 2.5 min on
    assert ui.waiter.armed[0][0]() is True
    card = ui.return_card
    assert card is not None and card.learn.ref == "pb:12"                      # the success is still announced
    assert ui.island.lesson_available is False                                 # ...but the question's card never saves it
    assert stamps and stamps[-1] == datetime.fromtimestamp(NOW + 10)           # "Done · <when the run ended>"
    card.close()


def test_no_late_card_comes_up_over_a_request_still_going(qapp, monkeypatch, store):
    pages = [{"available": True, "learned": []}]
    ui = _ui(qapp, monkeypatch, lambda: pages[-1])
    monkeypatch.setattr("time.time", lambda: NOW + 10)
    ui._queue_return_card(14, "Closed it.", "done", {"started": __import__("time").monotonic() - 10,
                                                    "calls": CLICK_ONLY})
    ui._on_started("worker", 15, "tidy my downloads")                          # still going
    pages.append({"available": True, "learned": [{"id": "pb:14", "kind": "procedure", "run_at": NOW,
                  "title": "close the lyrics view in Spotify", "learned_at": NOW + 150, "why": ""}]})
    ui.waiter.armed[0][0]()
    assert ui.return_card is None and ui.island.lesson_available is False


# -- review: the island's "using your lesson" chip never names a quiet note --------------------------------------------


def test_the_island_never_names_a_pitfall_as_your_lesson(qapp, monkeypatch, store):
    from test_island import _island

    from yuki.ui import app as app_mod

    learn_failed(store, Worker(store))
    pit = P.live(store, ["pitfall"])[0]
    ui = _ui(qapp, monkeypatch, lambda: P.workflows(store))
    got: list[dict] = []
    ui._lessons = SimpleNamespace(done=SimpleNamespace(emit=got.append))
    app_mod.YukiUi._fetch_lessons(ui)
    assert got and got[0][pit.ref] == ""                                       # known, and quiet
    isl = _island(qapp)
    try:
        isl.strip.start_run(16, steps=0)
        isl.strip.run_step(16, "Clicking Lyrics", 1)
        isl.step_meta(16, "click", {"name": "Lyrics", "playbook": pit.ref})
        isl.step_meta(16, "click", {"name": "Close", "playbook": "pb:999"})
        isl.set_lessons({**got[0], "pb:999": "close it with the Close lyrics button"})
        assert isl.lesson_refs() == []
        one = SimpleNamespace(label="Clicking — Lyrics", name="click", running=False, ok=True, key="s1", result="",
                              seconds=lambda: 0.4)
        item = SimpleNamespace(real_steps=lambda: [one], step_times=lambda: ({}, None))
        isl.model = SimpleNamespace(item_for_job=lambda _id: item)
        chips = [c for st in isl._steps(isl.inputs.run) for c in st["chips"]]
        assert chips == [("lesson", "using your lesson: close it with the Close lyrics button")]
    finally:
        isl.stop()
