"""appClean (4 Oct, docs/research/revamp-2026-10-02/59-build-appClean.md): clean what practice memory has saved, and
let the portrait's bar pass include pinned facts.

The user: "theres too many spotify lessons in 'How you do things' ... maybe fix and clean things up on whatever it has
saved up till now?" - and of a saved habit (close the lyrics view first, one action at a time): "No, I would not do it
like this."

A way learned from a failed run is retired by code; near-duplicates merge into the one that worked; a way the user
said they do not want goes only with their words; pitfalls stay hidden and are capped per app; kept procedures become
a title and numbered steps; the whole pass is a dry run, a saved plan, an apply and an undo. The bar pass retires
failing pinned facts only when asked, their lasting line keeping the pin.

Offline: temp databases, a word-bag stand-in for the embedder, stand-ins for the model calls; nothing touches the live
desktop, the real memory or the network. Names, apps' notes and words are invented."""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_appLearn import lyrics_run
from test_c_practice import NOW, Worker, step, store, vec  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import practice_clean as C
from yuki.memory.api import MemoryClient

USER_WORDS = "No, I would not do it like this. I just drag the volume slider."
SPOTIFY = ["spotify"]


def _way(store: Any, intent: str, steps: list[str], events: list[dict[str, Any]], *, kind: str = "procedure",
         origin: str = "postmortem", apps: list[str] = SPOTIFY, text: str | None = None, at: float = NOW) -> int:
    body: dict[str, Any] = {"steps": steps} if kind == "procedure" else {"text": text or intent}
    return P.add_playbook(store, {"kind": kind, "intent": intent, "apps": apps, "body": body, "origin": origin},
                          events=[{"at": at, **e} for e in events], vector=vec(intent), now=at)


def seed(store: Any) -> dict[str, Any]:
    """Invented Spotify notes like the ones that piled up: a way from a stopped run, the way that worked (wordy), the
    same way seen in what the user does, a saved habit describing the method the user does not want, an old note
    never confirmed, seven pitfalls of one app, a style note and a way of another app."""
    ids: dict[str, Any] = {}
    failed = P.record_run(store, lyrics_run("worker-1"))["run_key"]
    good = P.record_run(store, lyrics_run("worker-2", at=NOW + 600, outcome="final", words="turn spotify all the way up",
                                          steps=[step("focus_window", app="spotify", target='window "Spotify"'),
                                                 step("drag", app="spotify", target='Slider "Volume"',
                                                      result="the slider is at the top")]))["run_key"]
    ids["failed"] = _way(store, "close the lyrics then drag the volume", ["Click the Lyrics button", "Drag the slider"],
                         [{"type": "observed", "run_key": failed}])
    ids["good"] = _way(store, "Drag Spotify's volume slider all the way to the right",
                       ["Focus the Spotify window to ensure it is in front",
                        "Locate the volume slider control by role Slider and name containing 'Volume' in the bar",
                        "Drag the volume slider all the way to the right (toward maximum)"],
                       [{"type": "observed", "run_key": good}, {"type": "succeeded", "run_key": good},
                        {"type": "ran_ok", "run_key": "later-run"}])
    ids["seen"] = _way(store, "set spotify volume to max", ["Drag the Volume slider right"],
                       [{"type": "observed_day", "run_key": None}], origin="miner")
    ids["saved"] = _way(store, "Adjust Spotify volume slider safely", [], [
        {"type": "observed", "run_key": "knowhow:10"}, {"type": "taught", "refs": {"via": "playbook page"}},
        {"type": "user_saved", "refs": {"via": "playbook page"}}], kind="habit", origin="knowhow",
        text="Close the lyrics view by clicking the lyrics button, then drag the volume slider. Do one action at a "
             "time and wait for the UI to settle.")
    ids["old"] = _way(store, "Verify Spotify volume is fully set", ["Look at the slider", "Ask the user"],
                      [{"type": "observed", "run_key": "knowhow:8"}], origin="knowhow")
    pits = []
    for i in range(7):
        events = [{"type": "observed", "run_key": f"pit-run-{i}-{n}"} for n in range(max(1, i))]
        if i == 0:
            events.append({"type": "taught", "run_key": "pit-run-0-0", "refs": {"quote": "not the lyrics again"}})
        pits.append(P.add_playbook(store, {"kind": "pitfall", "intent": f"spotify toggle mistake {i}", "apps": SPOTIFY,
                                           "body": {"text": f"clicking the toggle did nothing ({'abcdefg'[i]})",
                                                    "instead": "ask the user first"}},
                                   events=[{"at": NOW, **e} for e in events], vector=vec(f"spotify toggle {i}"),
                                   now=NOW))
    ids["pits"] = pits
    ids["style"] = P.add_playbook(store, {"kind": "style", "intent": "emails in English", "body": {"text": "English"}},
                                  events=[{"type": "taught", "run_key": "r-style", "refs": {"quote": "in English"}}],
                                  vector=vec("emails in English"), now=NOW)
    ids["discord"] = _way(store, "Tag someone in a Discord channel",
                          ["Ctrl+K, the channel's name, Enter", "Type @ and the name, accept the suggestion"],
                          [{"type": "observed", "run_key": "r-tag"}, {"type": "succeeded", "run_key": "r-tag"}],
                          apps=["discord"])
    return ids


def answer(ids: dict[str, Any], *, quote: str = "No, I would not do it like this.") -> dict[str, Any]:
    """What the model is asked to say about the seed: the observed way kept over the one that worked (code swaps it),
    two pitfalls of one mistake merged, the saved habit retired with the user's words, the way that worked reshaped."""
    p = ids["pits"]
    return {"merge": [{"keep": ids["seen"], "drop": [ids["good"]], "why": "the same job"},
                      {"keep": p[6], "drop": [p[5]], "why": "the same mistake"}],
            "retire": [{"id": ids["saved"], "category": "unwanted", "quote": quote, "why": "the user does not do this"}],
            "reshape": [{"id": ids["good"], "title": "Turn Spotify all the way up",
                         "steps": ["Bring Spotify to the front", "Find the Volume slider in the bar",
                                   "Drag it all the way to the right"], "avoid": ""}]}


def live_ids(store: Any) -> set[int]:
    return {o.id for o in P.live(store, include_disabled=True)}


# -- (1) provenance, merges, the user's words, the cap -----------------------------------------------------------------


def test_provenance_is_measured_from_the_ledger_and_the_runs(store):
    ids = seed(store)
    prov = {k: P.provenance(store, P.get(store, ids[k]))[0] for k in ("failed", "good", "seen", "saved", "old")}
    assert prov == {"failed": "failed", "good": "success", "seen": "observed", "saved": "user", "old": "unconfirmed"}
    assert P.provenance(store, P.get(store, ids["pits"][0]))[0] == "lesson"


def test_the_clean_up_retires_failure_ways_merges_into_the_one_that_worked_and_hides_capped_pitfalls(store):
    ids = seed(store)
    before = live_ids(store)
    worker = Worker(store, [answer(ids)])
    res = C.CleanPass(worker, user_words=[USER_WORDS]).run(now=NOW + 3600)
    assert res.ok, res.error
    # the failure-derived way and the never-confirmed note are retired by code and never shown to the model
    sent = worker.calls[0]["user"]
    assert f"pb:{ids['failed']}]" not in sent and f"pb:{ids['old']}]" not in sent and USER_WORDS in sent
    cats = {d["id"]: (d["action"], d["category"], d["by"]) for d in res.decisions if d["action"] != "reshape"}
    assert cats[ids["failed"]] == ("retire", "from_failure", "code")
    assert cats[ids["old"]] == ("retire", "unconfirmed", "code")
    assert cats[ids["saved"]] == ("retire", "unwanted", "model")
    # the duplicate merges into the way that worked, not the one the model named
    assert cats[ids["seen"]] == ("merge", "duplicate", "model") and ids["good"] not in cats
    assert any("stands stronger" in r for r in res.rejected)
    live = live_ids(store)
    for k in ("failed", "old", "saved", "seen", "good"):
        assert ids[k] not in live
    [new] = [o for o in P.live(store, ["procedure"]) if o.apps == SPOTIFY]
    assert new.intent == "Turn Spotify all the way up" and len(new.steps) == 3
    assert P.get(store, ids["seen"]).superseded_by == new.id              # merged into the survivor's new version
    assert P.get(store, ids["good"]).superseded_by == new.id              # its old shape is history
    assert {e.type for e in P.ledger(store, new.id)} >= {"succeeded", "ran_ok", "cleaned"}   # the ledger moved
    # pitfalls: the merged one and the two weakest past the cap of 4 are retired; the user's words keep theirs
    p = ids["pits"]
    pits = {o.id for o in P.live(store, ["pitfall"])}
    assert pits == {p[0], p[3], p[4], p[6]}
    assert P.get(store, p[5]).superseded_by == p[6]
    assert {d["id"] for d in res.decisions if d["category"] == "over_cap"} == {p[1], p[2]}
    # ...and stay hidden: never on the Playbook page, at most three reach a request in that app
    page = P.workflows(store)
    shown = page["procedures"] + page["proposals"]
    assert sorted(c["title"] for c in shown) == ["Tag someone in a Discord channel", "Turn Spotify all the way up"]
    [spot] = [c for c in shown if "Spotify" in c["title"]]
    assert spot["steps"] == ["Bring Spotify to the front", "Find the Volume slider in the bar",
                             "Drag it all the way to the right"]
    assert not any(c["kind"] == "pitfall" for k in ("procedures", "lessons", "proposals", "learned") for c in page[k])
    from yuki.ui.web_data import build_playbook

    view = json.dumps(build_playbook(MemoryClient(store)), default=str)
    assert "lyrics" not in view.lower() and "toggle" not in view.lower()
    assert len(P.relevant(store, query_vec=vec("spotify toggle"), apps_in_play=["Spotify.exe"])["pitfalls"]) <= 3
    # the style note and the other app's way are untouched
    assert {ids["style"], ids["discord"]} <= live
    # the report is counts only
    rep = json.dumps(res.report())
    assert "Spotify all the way" not in rep and "lyrics" not in rep.lower()
    assert res.counts["spotify"] == {"before": 12, "kept": 5, "merged": 2, "retired": 5, "reshaped": 1}
    assert res.written == {"reshaped": 1, "merged": 2, "retired": 5}
    assert len(before) - len(live) == 7


def test_a_saved_way_goes_only_with_the_users_own_words_given_to_the_pass(store):
    ids = seed(store)
    # no words given: the user's Save stands, whatever the model says
    worker = Worker(store, [answer(ids)])
    res = C.CleanPass(worker).run(now=NOW + 3600, dry_run=True)
    assert ids["saved"] not in {d["id"] for d in res.decisions}
    assert any("saved it" in r for r in res.rejected)
    # words given, but the quote is not theirs
    worker = Worker(store, [answer(ids, quote="Never close the lyrics view.")])
    res = C.CleanPass(worker, user_words=[USER_WORDS]).run(now=NOW + 3600, dry_run=True)
    assert ids["saved"] not in {d["id"] for d in res.decisions}
    # a saved way is never retired as merely "not useful"
    bad = answer(ids)
    bad["retire"] = [{"id": ids["saved"], "category": "not_useful", "why": "?"}]
    res = C.CleanPass(Worker(store, [bad]), user_words=[USER_WORDS]).run(now=NOW + 3600, dry_run=True)
    assert ids["saved"] not in {d["id"] for d in res.decisions}


def test_a_dry_run_writes_nothing_and_its_saved_plan_is_applied_exactly(store, tmp_path):
    ids = seed(store)
    before = live_ids(store)
    worker = Worker(store, [answer(ids)])
    dry = C.CleanPass(worker, user_words=[USER_WORDS]).run(now=NOW + 3600, dry_run=True)
    assert dry.ok and dry.run_id is None and live_ids(store) == before
    assert store._query("SELECT count(*) FROM portrait_runs")[0][0] == 0
    path = C.save_plan(store, dry.decisions, tmp_path / "plan.json")
    raw = path.read_text(encoding="utf-8")
    assert "Spotify" not in raw and "lyrics" not in raw.lower()             # encrypted with memory's key
    plan = C.load_plan(store, path)
    calls = len(worker.calls)
    res = C.CleanPass(worker).run(now=NOW + 3700, plan=plan)
    assert res.ok and len(worker.calls) == calls                            # no new judgement
    assert res.written == {"reshaped": 1, "merged": 2, "retired": 5}
    assert store._query("SELECT kind FROM portrait_runs WHERE id=?", (res.run_id,))[0][0] == C.CLEAN_KIND
    with pytest.raises(ValueError):
        (tmp_path / "other.json").write_text('{"kind": "yuki-bar-plan"}', encoding="utf-8")
        C.load_plan(store, tmp_path / "other.json")


def test_undo_takes_the_whole_pass_back(store):
    ids = seed(store)
    before = live_ids(store)
    good_events = len(P.ledger(store, ids["good"]))
    res = C.CleanPass(Worker(store, [answer(ids)]), user_words=[USER_WORDS]).run(now=NOW + 3600)
    [new] = [o for o in P.live(store, ["procedure"]) if o.apps == SPOTIFY]
    out = C.undo(store, res.run_id, now=NOW + 4000)
    assert out["reshapes_undone"] == 1 and out["diverged"] == [] and not out["already"]
    assert live_ids(store) == before
    old = P.get(store, ids["good"])
    assert old.valid_to is None and old.superseded_by is None and old.intent.startswith("Drag Spotify")
    assert len(P.ledger(store, ids["good"])) >= good_events                  # its ledger is back
    assert P.get(store, new.id).valid_to is not None
    assert C.undo(store, res.run_id)["already"] is True


def test_reshapes_are_checked_never_more_steps_or_new_numbers(store):
    ids = seed(store)
    items = {it.obj.id: it for it in C.gather(store, P.live(store))}
    longer = {"merge": [], "retire": [], "reshape": [{"id": ids["good"], "title": "Turn it up",
                                                      "steps": ["a", "b", "c", "d"]}]}
    assert C.check(longer, items, []).reshape == []
    numbers = {"merge": [], "retire": [], "reshape": [{"id": ids["good"], "title": "Turn it up",
                                                       "steps": ["Click at 1887 then drag"]}]}
    assert C.check(numbers, items, []).reshape == []
    habit = {"merge": [], "retire": [], "reshape": [{"id": ids["saved"], "title": "x", "steps": ["y"]}]}
    assert C.check(habit, items, []).reshape == []                           # only procedures are reshaped
    mixed = {"merge": [{"keep": ids["good"], "drop": [ids["pits"][0]], "why": "?"}], "retire": [], "reshape": []}
    assert C.check(mixed, items, []).merge == []                             # a way never merges with a pitfall


def test_the_daily_cap_retires_the_weakest_pitfalls_and_can_be_taken_back(store):
    ids = seed(store)
    p = ids["pits"]
    over = P.cap_pitfalls(store, now=NOW + 86400)
    assert set(over) == {p[1], p[2], p[3]}                                   # the user's words and the strongest stay
    assert {o.id for o in P.live(store, ["pitfall"])} == {p[0], p[4], p[5], p[6]}
    day = P._day(NOW + 86400)
    out = C.undo(store, f"daily-{day}", now=NOW + 86500)
    assert out["restored"] == 3 and len(P.live(store, ["pitfall"])) == 7


def test_the_practice_worker_caps_pitfalls_once_a_day(store):
    from yuki.memory import practice_learn as L

    ids = seed(store)
    worker = Worker(store)
    pw = L.PracticeWorker(store, worker, clock=lambda: NOW + 86400)
    out = pw.tick()
    assert len(out.get("capped") or []) == 3
    assert len(P.live(store, ["pitfall"])) == P.PITFALLS_PER_APP


# -- (3) the bar pass with pinned facts ----------------------------------------------------------------------------------


def test_the_bar_pass_retires_a_failing_pinned_fact_only_when_asked_and_keeps_the_pin():
    import tempfile
    from pathlib import Path

    from test_portrait_bar import RENDER, T0, _facts, fail, worker_with
    from yuki.memory.dreaming import BarPass
    from yuki.memory.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        st = Store.open(Path(tmp) / "memory.db")
        try:
            _j, ids = _facts(st)
            quota, mix, _sister, _work, _habit, advice, _loop = ids
            lasting = "The user runs coding agents in parallel chats."
            verdicts = [{"failing": [fail(f"F{mix}", "usage_metrics"),
                                     fail(f"F{advice}", "ai_tool_output", lasting)]}]
            # without the option: reported, never retired (as before)
            worker, _c = worker_with(st, {"judge_bar": list(verdicts), **RENDER})
            dry = BarPass(worker).run(now=T0 + 7200, dry_run=True)
            assert [p["id"] for p in dry.pinned] == [advice] and advice not in {d["id"] for d in dry.decisions}
            # with it: retired like any other, its lasting line pinned, the diary says so
            worker, _c = worker_with(st, {"judge_bar": list(verdicts), **RENDER})
            res = BarPass(worker, include_pinned=True).run(now=T0 + 7300)
            assert res.ok, res.error
            assert res.retired == 2 and [p["id"] for p in res.pinned] == [advice]
            current = {f.id: f for f in st.portrait_facts()}
            assert advice not in current and mix not in current
            [new] = [f for f in current.values() if f.text == lasting]
            assert new.pinned
            assert any("pinned" in d["text"] for d in st.changelog(pass_="bar") if advice in d["fact_ids"])
            # undo: the pinned fact is back, still pinned; its lasting line ends
            undone = BarPass(worker).undo(res.run_id)
            assert undone["restored"] == 2 and undone["ended"] == 1
            back = {f.id: f for f in st.portrait_facts()}
            assert advice in back and back[advice].pinned and new.id not in back
        finally:
            st.close()


def test_a_saved_bar_plan_with_a_pinned_fact_needs_the_option_again(tmp_path):
    from pathlib import Path

    from test_portrait_bar import RENDER, T0, _facts, fail, worker_with
    from yuki.memory.dreaming import BarPass, load_plan, save_plan
    from yuki.memory.store import Store

    st = Store.open(Path(tmp_path) / "memory.db")
    try:
        _j, ids = _facts(st)
        advice = ids[5]
        worker, _c = worker_with(st, {"judge_bar": [{"failing": [fail(f"F{advice}", "ai_tool_output")]}], **RENDER})
        dry = BarPass(worker, include_pinned=True).run(now=T0 + 7200, dry_run=True)
        assert dry.decisions[0]["pinned"] is True
        plan = load_plan(st, save_plan(st, dry.decisions, tmp_path / "plan.json"))
        res = BarPass(worker).run(now=T0 + 7300, plan=plan)                  # without the option: kept, reported
        assert res.retired == 0 and [p["id"] for p in res.pinned] == [advice]
        res = BarPass(worker, include_pinned=True).run(now=T0 + 7400, plan=plan)
        assert res.retired == 1 and advice not in {f.id for f in st.portrait_facts()}
    finally:
        st.close()
