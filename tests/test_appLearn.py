"""appLearn (4 Oct, docs/research/revamp-2026-10-02/54-build-appLearn.md): only a verified success is learned and
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
    return ui


CLOSED = [{"name": "run_powershell", "ok": True, "input": {"what": "close the lyrics", "_about": {"changes": True}}}]


def test_a_stopped_runs_return_card_has_no_learned_box_and_reads_nothing(qapp, monkeypatch, store):
    worker = Worker(store)
    learn_failed(store, worker)
    ui = _ui(qapp, monkeypatch, lambda: P.workflows(store))
    ui._queue_return_card(4, "Stopped.", "stopped", {"started": 0.0, "calls": CLOSED})
    card = ui.return_card
    assert card is not None and card.learn.isHidden() and ui.island.lesson_available is False
    assert ui.reads == [] and ui.timers.kept == []                              # no memory read, no late read
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
    monkeypatch.setattr("time.time", lambda: NOW + 202)
    ui._queue_return_card(5, "Playing it.", "done", {"started": __import__("time").monotonic() - 22, "calls": CLOSED})
    assert ui.reads == ["workflows"] and ui.timers.kept == []                   # found at once: no late read
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


def test_a_done_run_learned_later_gets_its_learned_box_from_one_late_read(qapp, monkeypatch, store):
    """The learner writes a run's way after its follow-up window: the run's card asks once more, later, and shows it."""
    pages = [{"available": True, "learned": []}]
    ui = _ui(qapp, monkeypatch, lambda: pages[-1])
    monkeypatch.setattr("time.time", lambda: NOW + 30)
    ui._queue_return_card(6, "Playing it.", "done", {"started": __import__("time").monotonic() - 20, "calls": CLOSED})
    assert ui.return_card.learn.isHidden() and len(ui.timers.kept) == 1
    ms, late = ui.timers.kept[0]
    assert ms == 150_000
    pages.append({"available": True, "learned": [{"id": "pb:9", "kind": "procedure", "title": "play one of your "
                  "playlists in Spotify", "learned_at": NOW + 140, "why": ""}]})
    late()                                                                      # the one late read
    assert not ui.return_card.learn.isHidden() and ui.return_card.learn.ref == "pb:9"
    assert ui.island.lesson_available is True and ui._return_learned["id"] == "pb:9"
    # a later run's card is never given another run's learning
    ui._return_card_id = 7
    ui._on_return_ready({"late": True, "id": 6, "learned": {"id": "pb:10", "text": "x"}, "view": {}, "took": ""})
    assert ui.return_card.learn.ref == "pb:9"
    ui.return_card.close()


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
    card = learned_for_card(page, NOW + 600)
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
