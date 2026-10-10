"""rMiner (plan rev 4, Phase R "The miner"; D3, D7, D14, D18; Appendix B.3, E): the code-only routine miner.

Offline: temp databases with invented rows (app keys, times; no real names), invented skills unless a test says it
uses the shipped connection files. Nothing reads the desktop, a microphone or the real memory; no model is called.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from yuki.memory import routines as R
from yuki.memory.store import MIGRATIONS, FactChange, ModelCall, NewFact, PortraitRunStats, Store, TimelineRow

MON = date(2026, 9, 14)   # a Monday


@dataclass(frozen=True)
class Slot:
    name: str
    routine_key: bool = False


@dataclass(frozen=True)
class Skill:
    key: str
    routine_on: tuple
    offerable: bool = True
    kind: str = "open"
    slots: tuple = field(default_factory=tuple)


MEET = Skill("meet.open", (("call_join", "zen"),), True, "open")
VOICE = Skill("discord.voice", (("call_join", "discord"),), False, "change")
SKILLS = (MEET, VOICE)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> float:
    return (datetime(d.year, d.month, d.day, hh) + timedelta(minutes=mm, seconds=ss)).timestamp()


def days(first: date, n: int, *, weekdays: bool = False) -> list[date]:
    out = [first + timedelta(days=i) for i in range(n)]
    return [d for d in out if not weekdays or d.weekday() < 5]


def present(store, d: date, spans=((7, 0, 13, 0), (18, 0, 23, 30)), *, process="code", by_yuki=False):
    for h1, m1, h2, m2 in spans:
        start = at(d, h1, m1)
        end = at(d, h2, m2) if (h2, m2) > (h1, m1) else at(d + timedelta(days=1), h2, m2)
        store.save_timeline(TimelineRow(
            id=None, started_at=start, ended_at=end, state="present", process=process, app=process.title(),
            title=None, host=None, path=None, page_key=None, active_s=end - start, passive_s=0.0, away_s=0.0,
            media_s=0.0, media_other={}, withheld=None, segments=1, fullscreen=False, meeting=None, mic_s=0.0,
            by_yuki=by_yuki, yuki_request=None))


_ids = iter(range(1, 10_000_000))


def moment(store, t: float, *, app="zen", kind="call_join", evidence="screen", by="user", service="Google Meet",
           digest=None):
    store.save_moments([{"at": t, "kind": kind, "app": app, "service": service, "evidence": evidence,
                         "by": by, "source": "test", "source_id": next(_ids), "entity_digest": digest,
                         "facts": {"seen_at": t} if evidence != "hold" else {}}])


def e1(store, first=MON, weeks=3, *, holds=(), skip=(), minute=58, jitter=(0, 2, -1, 1, -2)):
    """A weekday meeting around 08:58 for ``weeks`` weeks: seen joins, holds on ``holds`` days, none on ``skip``."""
    out = []
    for i, d in enumerate(days(first, weeks * 7, weekdays=True)):
        if d in skip:
            continue
        t = at(d, 8, minute) + 60 * jitter[i % len(jitter)]
        moment(store, t, evidence="hold" if d in holds else "screen")
        out.append(t)
    return out


def setup_presence(store, first=MON, n=42, **kw):
    for d in days(first, n):
        present(store, d, **kw)


def miner(store, log=None):
    return R.RoutineMiner(store=store, skills=SKILLS, log=log)


def run_days(store, first: date, last: date, log=None):
    out = []
    d = first
    while d <= last:
        res = miner(store, log).run(now=R.day_start(d))
        assert res.ok, res.error
        out.append((d, res))
        d += timedelta(days=1)
    return out


# -- the tables (migration 42) -------------------------------------------------------------------------------------


def test_migration_42_adds_the_routine_tables_and_is_safe_to_open_again(tmp_path):
    s = Store.open(tmp_path / "m.db")
    s.close()
    s = Store.open(tmp_path / "m.db")
    try:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS)
        cols = {r[1] for r in s._query("PRAGMA table_info(routines)")}
        assert cols == set(Store.ROUTINE_COLUMNS) | {"id"}
        assert {r[1] for r in s._query("PRAGMA table_info(routine_events)")} == {
            "id", "routine_id", "at", "kind", "nudge_id", "detail"}
        assert {"routines_key"} <= {r[1] for r in s._query("PRAGMA index_list(routines)")}
        idx = {r[1] for r in s._query("PRAGMA index_list(routine_events)")}
        assert {"routine_events_r", "routine_events_at"} <= idx
    finally:
        s.close()


def test_routine_store_helpers_round_trip(store):
    rid = store.save_routine({"key": "meet.open|zen", "skill_id": "meet.open", "app": "zen", "trigger": "clock",
                              "dayset": "weekdays", "at_min": 538.0, "state": "active", "created_at": 1.0,
                              "updated_at": 1.0}, events=[("state", 1.0, {"state_to": "active"})])
    assert store.routine(rid)["offer_mode"] == "early"
    assert store.set_routine_mode(rid, "late", 2.0, detail={"why": "self_served"})
    assert not store.set_routine_mode(rid, "late", 3.0)
    ev = store.routine_events(rid)
    assert [e["kind"] for e in ev] == ["state", "mode_change"]
    assert ev[1]["detail"] == {"mode_from": "early", "mode_to": "late", "why": "self_served"}
    assert store.routines(states=["active"], key="meet.open|zen")[0]["offer_mode"] == "late"
    assert store.routines(skill="discord.voice") == []


# -- moments in, keys out ------------------------------------------------------------------------------------------


def test_one_key_from_several_sources_and_day_onsets(store):
    d = MON
    setup_presence(store, d, 1)
    moment(store, at(d, 9, 0, 0))                     # the timeline's row
    moment(store, at(d, 9, 0, 47))                    # the calls row 47 s later: the same join
    moment(store, at(d, 9, 30), evidence="hold")      # still the same morning (under 2 h since the last)
    moment(store, at(d, 11, 31))                      # 2 h after the last one: a new onset
    inp = R.keyed_onsets(store, at(d, 0), at(d + timedelta(days=1), 4), skills=SKILLS)
    assert list(inp.onsets) == ["meet.open|zen"]
    assert inp.onsets["meet.open|zen"] == [at(d, 9), at(d, 11, 31)]
    assert inp.keys["meet.open|zen"].offerable and inp.keys["meet.open|zen"].skill == "meet.open"
    assert inp.services["meet.open|zen"]["Google Meet"] == 4


def test_unmapped_apps_never_make_a_key_and_yukis_own_runs_never_count(store):
    d = MON
    setup_presence(store, d, 1)
    for app in ("fl64", "systemsettings", "valorant-win64-shipping", "slack"):
        moment(store, at(d, 9), app=app)
    moment(store, at(d, 10), by="yuki")
    inp = R.keyed_onsets(store, at(d, 0), at(d + timedelta(days=1), 4), skills=SKILLS)
    assert inp.onsets == {}
    assert inp.unmapped == {"call_join|fl64": 1, "call_join|systemsettings": 1,
                            "call_join|valorant-win64-shipping": 1, "call_join|slack": 1}
    assert inp.by_yuki == 1


def test_two_skills_listing_one_moment_is_never_guessed(store):
    d = MON
    setup_presence(store, d, 1)
    moment(store, at(d, 9))
    twin = Skill("other.open", (("call_join", "zen"),))
    inp = R.keyed_onsets(store, at(d, 0), at(d + timedelta(days=1), 4), skills=(MEET, twin))
    assert inp.onsets == {} and inp.unmapped == {"call_join|zen|ambiguous": 1}


def test_a_routine_key_slot_keys_by_the_entity_digest(store):
    d = MON
    setup_presence(store, d, 1)
    playlist = Skill("spotify.playlist", (("media_context", "spotify"),), True, "change", (Slot("name", True),))
    moment(store, at(d, 22), app="spotify", kind="media_context", digest="a" * 64, service=None)
    moment(store, at(d, 23), app="spotify", kind="media_context", digest=None, service=None)
    inp = R.keyed_onsets(store, at(d, 0), at(d + timedelta(days=1), 4), skills=(playlist,))
    assert list(inp.onsets) == ["spotify.playlist|spotify|" + "a" * 64]
    assert inp.unmapped == {"media_context|spotify|no_entity": 1}
    assert R._short_key(next(iter(inp.onsets))) == "spotify.playlist|spotify|" + "a" * 12


def test_the_shipped_connection_files_map_the_meeting_and_the_voice_channel(store):
    d = MON
    setup_presence(store, d, 1)
    moment(store, at(d, 9))
    moment(store, at(d, 20), app="discord", service="Discord")
    moment(store, at(d, 21), app="fl64")
    inp = R.keyed_onsets(store, at(d, 0), at(d + timedelta(days=1), 4))
    assert set(inp.onsets) == {"meet.open|zen", "discord.voice|discord"}
    assert inp.keys["meet.open|zen"].offerable and not inp.keys["discord.voice|discord"].offerable
    assert inp.unmapped == {"call_join|fl64": 1}


# -- the bars ------------------------------------------------------------------------------------------------------


def _mine(store, until_day: date, skills=SKILLS):
    until = R.day_start(until_day)
    return R.mine(R.keyed_onsets(store, until - 28 * 86400, until, skills=skills), until)


def test_e1_shape_passes_every_bar_on_weekdays(store):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    got = _mine(store, MON + timedelta(days=21))
    [w] = got.passed
    assert (w.key, w.dayset, w.hits, w.eligible) == ("meet.open|zen", "weekdays", 15, 15)
    assert R.hm(w.at_min) == "08:58" and w.mad <= 2 and w.p < 0.05 and w.stable and w.lift >= 3


def test_d7_holds_count_only_inside_a_window_with_three_seen_days(store):
    setup_presence(store, MON, 28)
    wk = days(MON, 14, weekdays=True)   # 10 weekdays
    e1(store, weeks=2, holds=set(wk[2:]))                  # 2 seen days + 8 holds: holds never make a window
    assert _mine(store, MON + timedelta(days=14)).passed == []


def test_d7_corroborated_holds_fill_in_the_days(tmp_path):
    def build(holds):
        s = Store.open(tmp_path / f"h{len(holds)}.db")
        setup_presence(s, MON, 21)
        wk = days(MON, 14, weekdays=True)
        skip = set(wk[i] for i in (1, 6))
        e1(s, weeks=2, holds=set(wk[i] for i in holds), skip=skip)
        return s

    s = build((0, 3, 5))   # 5 seen days + 3 corroborated holds: 8 hits
    try:
        [w] = _mine(s, MON + timedelta(days=14)).passed
        assert (w.hits, w.seen_hits) == (8, 5)
    finally:
        s.close()


def test_the_permutation_test_rejects_a_window_the_presence_explains(store):
    for d in days(MON, 28):
        present(store, d, spans=((8, 30, 9, 30),))   # only ever at the PC for that hour
        if d.weekday() < 5:
            moment(store, at(d, 8, 50) + random.Random(d.toordinal()).uniform(0, 20 * 60))
    got = _mine(store, MON + timedelta(days=28))
    assert got.passed == []
    near = got.near["meet.open|zen"]
    assert near.p is not None and near.p >= R.P_MAX and "p" in near.failed


def test_random_activity_never_passes(store):
    rng = random.Random(7)
    setup_presence(store, MON, 28)
    for d in days(MON, 28):
        for _ in range(2):
            h = rng.choice([7, 8, 9, 10, 11, 12, 18, 19, 20, 21, 22])
            moment(store, at(d, h, rng.randrange(60)))
    assert _mine(store, MON + timedelta(days=28)).passed == []


def test_two_windows_on_one_key_are_two_routines(store):
    setup_presence(store, MON, 28)
    for d in days(MON, 21, weekdays=True):
        moment(store, at(d, 9, 0))
        moment(store, at(d, 11, 30))
    got = _mine(store, MON + timedelta(days=21))
    assert sorted(R.hm(w.at_min) for w in got.passed) == ["09:00", "11:30"]
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    assert sorted(R.hm(r["at_min"]) for r in store.routines(states=["active"])) == ["09:00", "11:30"]


def test_a_routine_around_midnight_stays_one_window(store):
    for d in days(MON, 28):
        present(store, d, spans=((21, 0, 2, 0),))
    rng = random.Random(3)
    for d in days(MON, 21):
        moment(store, at(d, 23, 55) + rng.uniform(0, 10 * 60))   # 23:55 - 00:05, across midnight
    [w] = _mine(store, MON + timedelta(days=22)).passed
    assert w.dayset == "all" and w.hits >= 20 and w.mad <= 5
    assert R.hm(w.at_min) in ("23:58", "23:59", "00:00", "00:01", "00:02")


def test_a_day_still_under_way_is_never_a_miss(store):
    setup_presence(store, MON, 22)
    e1(store, weeks=3)
    until = at(MON + timedelta(days=21), 8, 0)   # Monday 08:00: today's 08:58 is still ahead
    inp = R.keyed_onsets(store, until - 28 * 86400, until, skills=SKILLS)
    st = R.window_stats(inp, "meet.open|zen", "weekdays", 538.0, until)
    assert st.misses_in_row == 0 and st.eligible == 15


# -- state: active, dormant, moved, retired, told --------------------------------------------------------------------


def test_a_routine_becomes_active_the_night_after_it_passes_and_writes_a_state_event(store):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    out = run_days(store, MON, MON + timedelta(days=21))
    first = next(d for d, res in out if any(e.get("state_to") == "active" for e in res.decisions))
    assert first == MON + timedelta(days=4)   # Friday night: 4 hits (Mon-Thu) on 4 days
    [row] = store.routines()
    assert row["state"] == "active" and row["trigger"] == "clock" and row["key"] == "meet.open|zen"
    assert row["active_since"] == R.day_start(first)
    assert [e["detail"]["state_to"] for e in store.routine_events(row["id"], kinds=["state"])] == ["active"]


def test_dormant_after_five_eligible_misses_then_retired_after_28_days(store):
    setup_presence(store, MON, 70)
    e1(store, weeks=3)
    out = run_days(store, MON + timedelta(days=21), MON + timedelta(days=70))
    states = [(d, e["state_to"], e.get("why")) for d, res in out for e in res.decisions if e.get("event") == "state"]
    to_dormant = [(d, why) for d, s, why in states if s == "dormant"]
    assert to_dormant and to_dormant[0][1] in ("misses_in_row", "rate")
    assert to_dormant[0][0] <= MON + timedelta(days=21 + 8)   # within 5 weekdays (plus a weekend) of the last hit
    retired = [d for d, s, _w in states if s == "retired"]
    last_hit = max(e1_days for e1_days in days(MON, 21, weekdays=True))
    assert retired and (retired[0] - last_hit).days in (28, 29)
    assert store.routines(states=["active", "dormant"]) == []


def test_a_routine_moved_an_hour_is_relearned_as_a_new_row(store):
    setup_presence(store, MON, 63)
    e1(store, weeks=3)
    e1(store, first=MON + timedelta(days=21), weeks=4, minute=58 + 60)   # from week 4: about 09:58
    run_days(store, MON, MON + timedelta(days=47))   # the night after the last new-time Friday
    rows = store.routines()
    old = [r for r in rows if abs(r["at_min"] - 538) < 30]
    new = [r for r in rows if abs(r["at_min"] - 598) < 30]
    assert old and new and old[0]["id"] != new[0]["id"]
    assert old[0]["state"] in ("dormant", "retired") and new[0]["state"] == "active"
    became = min(e["at"] for e in store.routine_events(new[0]["id"], kinds=["state"]))
    # eligible days start at its first hit; one permutation test per window a night (review L1) and the 15 old
    # onsets still in the 28 days make it the night after its 8th hit day here
    assert R.day_of(became) <= MON + timedelta(days=21 + 10)


def test_a_dormant_routine_passing_again_is_active_again(store):
    setup_presence(store, MON, 63)
    e1(store, weeks=3)
    e1(store, first=MON + timedelta(days=35), weeks=3)   # two weeks off, then back at the same time
    run_days(store, MON, MON + timedelta(days=56))
    [row] = store.routines()
    tos = [e["detail"]["state_to"] for e in store.routine_events(row["id"], kinds=["state"])]
    assert tos[:3] == ["active", "dormant", "active"] and row["state"] == "active"


def test_a_told_routine_is_active_after_two_seen_hits_never_on_holds(store):
    setup_presence(store, MON, 14)
    rid = R.tell(store, "meet.open", "zen", "weekdays", 9 * 60, label="Invented Standup", now=at(MON, 6))
    assert store.routine(rid)["state"] == "candidate" and store.routine_label(store.routine(rid)) == "Invented Standup"
    moment(store, at(MON, 9, 1), evidence="hold")
    moment(store, at(MON + timedelta(days=1), 9, 2))
    run_days(store, MON + timedelta(days=2), MON + timedelta(days=2))
    assert store.routine(rid)["state"] == "candidate"              # 1 seen hit (+1 hold)
    moment(store, at(MON + timedelta(days=2), 8, 59))
    run_days(store, MON + timedelta(days=3), MON + timedelta(days=3))
    row = store.routine(rid)
    assert row["state"] == "active" and row["confirmed_at"] == R.day_start(MON + timedelta(days=3))
    assert [e["kind"] for e in store.routine_events(rid)] == ["told", "state"]


def test_a_told_routine_the_miner_finds_keeps_its_label(store):
    setup_presence(store, MON, 28)
    rid = R.tell(store, "meet.open", "zen", "weekdays", 9 * 60 + 5, label="Invented Standup", now=at(MON, 6))
    e1(store, weeks=3)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    [row] = store.routines()
    assert row["id"] == rid and row["trigger"] == "told" and row["state"] == "active" and row["confirmed_at"]
    assert store.routine_label(row) == "Invented Standup" and R.hm(row["at_min"]) == "08:58"


def test_tell_refuses_an_unknown_dayset(store):
    with pytest.raises(ValueError):
        R.tell(store, "meet.open", "zen", "mondays", 540)


# -- the portrait's Routines lines from counts ----------------------------------------------------------------------


def _journal(store, text, *, at_):
    store.commit_batch(ModelCall(at=at_, thread_id=None, model="test", cost_usd=0.0),
                       [], [NewFact(at=at_, thread_id=None, app="Zen", host=None, fact=text, importance=5,
                                    nature="fact", source=None)])
    return max(j.id for j in store.journal_between(None, None))


def test_an_active_routine_gets_one_measured_fact_through_a_code_portrait_run(store):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    [f] = [f for f in store.portrait_facts(("routine",))]
    assert f.origin == R.MEASURED_ORIGIN and not f.pinned
    assert f.text.startswith("On 15 of the last 15 weekdays the user joined a Google Meet call in Zen at about 08:")
    assert f.summary.startswith("Google Meet call in Zen, weekdays ~8:") and len(f.summary.split()) <= 8
    assert f.shown.startswith("You join a Google Meet call in Zen around 8:") and " AM " in f.shown
    assert 0 < f.confidence <= 0.9
    run = store.portrait_runs(1)[0]
    assert (run["kind"], run["model"], run["cost_usd"], run["ops_add"], run["outcome"]) == ("routines", "code", 0.0,
                                                                                            1, "ok")
    assert store.routines()[0]["fact_id"] == f.id
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=23))   # same time, a day later: left alone
    assert [x.id for x in store.portrait_facts(("routine",))] == [f.id]


def test_the_fact_is_ended_when_the_routine_goes_dormant(store):
    setup_presence(store, MON, 42)
    e1(store, weeks=3)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=35))
    assert store.routines()[0]["state"] == "dormant"
    assert store.portrait_facts(("routine",)) == [] and store.routines()[0]["fact_id"] is None


def test_the_first_run_ends_the_old_sonnet_routine_facts_only(store):
    sonnet = store.start_portrait_run("routines", "sonnet", at=at(MON, 3))
    nightly = store.start_portrait_run("nightly", "sonnet", at=at(MON, 3))
    [(ok1, old)] = store.commit_portrait_changes(sonnet, [FactChange("ADD", None, "routine", "", "Meet 9 AM-1 PM",
                                                                     0.4, [], at(MON, 3))])
    [(ok2, meals)] = store.commit_portrait_changes(nightly, [FactChange("ADD", None, "routine", "", "Lunch at two",
                                                                        0.6, [], at(MON, 3))])
    assert ok1 and ok2
    res = miner(store).run(now=at(MON, 4))
    assert res.ok and res.mined["first_run_invalidated"] == 1
    assert [f.id for f in store.portrait_facts(("routine",))] == [meals]
    assert R.model_routine_facts(store) == []


def _measured_routine(store):
    """An active E1 routine with its measured line, and two more weeks of joins to keep it active."""
    setup_presence(store, MON, 42)
    e1(store, weeks=3)
    e1(store, first=MON + timedelta(days=21), weeks=2, minute=59)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    [row] = store.routines()
    assert row["state"] == "active" and row["fact_id"] is not None
    return row["fact_id"]


def test_a_line_the_user_said_is_not_right_is_never_written_again(store):
    from yuki.memory.api import MemoryClient

    fid = _measured_routine(store)
    got = MemoryClient(store).review_mark(f"fact:{fid}", "wrong")   # the review queue's "not right"
    assert got["ok"]
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=36))
    row = store.routines()[0]
    assert row["fact_off"] == 1 and row["fact_id"] is None
    assert [f for f in store.portrait_facts(("routine",)) if f.origin == R.MEASURED_ORIGIN] == []


def test_a_line_the_users_correction_rewrote_stays_theirs(store):
    fid = _measured_routine(store)
    run = store.start_portrait_run("correct", "test", at=time.time())
    [(ok, new)] = store.commit_portrait_changes(run, [FactChange(
        "UPDATE", fid, "routine", "", "The user's standup is at nine.", 0.9, [], time.time(), R.MEASURED_ORIGIN)])
    assert ok
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=29))
    row = store.routines()[0]
    assert row["fact_off"] == 1 and row["fact_id"] is None
    assert [f.id for f in store.portrait_facts(("routine",))] == [new]   # the corrected words stand


def _dream(store, change):
    """dreaming.py's own apply (the model's judgement stubbed by the change it would have made): a merge UPDATEs the
    kept fact with the model's words and keeps its origin, INVALIDATEs the others and points them at it; a retire
    INVALIDATEs; one diary line each."""
    from types import SimpleNamespace

    from yuki.memory.dreaming import Dreamer, DreamResult

    run = store.start_portrait_run("dream", "test", at=time.time())
    result = DreamResult(run)
    Dreamer(SimpleNamespace(store=store)).apply(run, [change], [], store.portrait_facts(), time.time(), result)
    assert result.applied == 1 and not result.rejected
    return result


def _dream_merge(store, keep, other, text):
    result = _dream(store, {"action": "merge", "ids": [keep, other], "keep": keep, "text": text, "importance": None,
                            "reason": "look-alikes"})
    return result.diary[0]["new_fact_ids"][0]


def _model_routine_fact(store, text="The user is usually on a call in the mornings."):
    run = store.start_portrait_run("nightly", "test", at=time.time())
    [(ok, fid)] = store.commit_portrait_changes(run, [FactChange("ADD", None, "routine", "", text, 0.6, [],
                                                                 time.time())])
    assert ok
    return fid


def test_a_dreaming_merge_that_keeps_the_measured_fact_is_replaced_by_counts_the_next_night(store):
    fid = _measured_routine(store)
    model = _model_routine_fact(store)
    merged = _dream_merge(store, fid, model, "The user joins a morning call most weekdays, often running late.")
    assert store.portrait_fact(merged).origin == R.MEASURED_ORIGIN   # the model's words posing as counts
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=22))
    row = store.routines()[0]
    assert not row["fact_off"] and row["fact_id"] not in (None, merged)
    [line] = store.portrait_facts(("routine",))
    assert line.id == row["fact_id"] and line.origin == R.MEASURED_ORIGIN
    assert line.text.startswith("On ") and "of the last" in line.text and "running late" not in line.text
    assert store.portrait_fact(merged).superseded_by == line.id    # the merged version's history leads here
    run = store.portrait_runs(1)[0]
    assert (run["kind"], run["model"], run["cost_usd"]) == ("routines", "code", 0.0)
    # it stays the miner's: a later night leaves it alone, and it still ends when the routine stops
    run_days(store, MON + timedelta(days=23), MON + timedelta(days=23))
    assert [f.id for f in store.portrait_facts(("routine",))] == [line.id]


def test_a_dreaming_merge_that_keeps_the_other_fact_gets_the_line_back(store):
    fid = _measured_routine(store)
    model = _model_routine_fact(store)
    merged = _dream_merge(store, model, fid, "The user is on calls in the mornings.")
    assert store.portrait_fact(merged).origin == "model"
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=22))
    row = store.routines()[0]
    [line] = store.portrait_facts(("routine",))
    assert line.id == row["fact_id"] and line.origin == R.MEASURED_ORIGIN and line.text.startswith("On ")
    assert store.portrait_fact(merged).valid_to is not None


def test_a_dreaming_retire_and_the_bars_lasting_line_are_undone(store):
    fid = _measured_routine(store)
    run = store.start_portrait_run("bar", "test", at=time.time())
    written = store.commit_portrait_changes(run, [
        FactChange("INVALIDATE", fid, "routine", "", "", None, [], time.time()),
        FactChange("ADD", None, "routine", "", "The user has morning calls.", 0.6, [], time.time(), "model")])
    lasting = written[1][1]
    store.add_diary(run, [{"pass": "bar", "action": "retire", "fact_ids": [fid], "new_fact_ids": [lasting],
                           "text": "retired"}])
    store.finish_portrait_run(run, PortraitRunStats(kind="bar", model="test", outcome="ok"))
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=22))
    [line] = store.portrait_facts(("routine",))
    assert line.origin == R.MEASURED_ORIGIN and line.id == store.routines()[0]["fact_id"]
    assert store.portrait_fact(lasting).superseded_by == line.id
    # a plain retire (no lasting line) is written again as a new line
    _dream(store, {"action": "retire", "ids": [line.id], "cites": [], "reason": "a one-off", "lasting": None})
    run_days(store, MON + timedelta(days=23), MON + timedelta(days=23))
    [again] = store.portrait_facts(("routine",))
    assert again.id != line.id and again.origin == R.MEASURED_ORIGIN and not store.routines()[0]["fact_off"]


def test_a_merged_line_of_a_routine_gone_dormant_just_ends(store):
    setup_presence(store, MON, 42)
    e1(store, weeks=3)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    fid = store.routines()[0]["fact_id"]
    model = _model_routine_fact(store)
    merged = _dream_merge(store, fid, model, "The user joins a morning call.")
    run_days(store, MON + timedelta(days=22), MON + timedelta(days=35))
    assert store.routines()[0]["state"] == "dormant"
    assert store.portrait_facts(("routine",)) == [] and store.portrait_fact(merged).valid_to is not None


def test_a_time_on_a_rounding_boundary_does_not_rewrite_the_line(store):
    row = {"dayset": "weekdays", "at_min": 537.4}
    sig = R._fact_sig(row)
    for at_min in (537.6, 535.0, 539.9, 532.5):   # 537.5 was a 5-minute boundary of the old rounding
        assert R._sig_holds(sig, dict(row, at_min=at_min))
    assert not R._sig_holds(sig, dict(row, at_min=542.4)) and not R._sig_holds(sig, dict(row, dayset="all"))
    assert not R._sig_holds("weekdays|108", row)   # an old-style signature: written again once


def test_the_portrait_validator_refuses_model_edits_to_measured_facts(store):
    from yuki.memory import portrait as P

    assert P.MEASURED_ORIGIN == R.MEASURED_ORIGIN
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    [f] = store.portrait_facts(("routine",))
    jid = _journal(store, "The user joins their standup at nine.", at_=time.time() - 60)
    journal = store.journal_between(None, None)
    worker = P.PortraitWorker(store, client=object())
    base = {"fact_id": f.id, "kind": "routine", "subject": "", "summary": None, "shown": None, "confidence": 0.7,
            "importance": 5, "journal_ids": [jid], "episode_ids": [], "based_on_facts": [], "reason": "r",
            "loop": None, "aspect": None}
    for op in ("UPDATE", "INVALIDATE"):
        changes, outcomes = worker._validate([dict(base, op=op, text="The user joins at 9:30.")],
                                             store.portrait_facts(), journal, time.time())
        assert changes == [] and "measured routine" in outcomes[0].status
    changes, outcomes = worker._validate([dict(base, op="NOOP", text="")], store.portrait_facts(), journal,
                                         time.time())
    assert changes == [] and outcomes[0].status == "noop"
    # a model routine fact is still the model's to change
    [(ok, mf)] = store.commit_portrait_changes(None, [FactChange("ADD", None, "routine", "", "Lunch at two", 0.6, [],
                                                                 time.time())])
    changes, _ = worker._validate([dict(base, fact_id=mf, op="UPDATE", text="Lunch at three.")],
                                  store.portrait_facts(), journal, time.time())
    assert len(changes) == 1


# -- the nightly pass: no model, the render, the logs ---------------------------------------------------------------


class _Worker:
    def __init__(self, store):
        self.store = store
        self._run_lock = threading.Lock()
        self._hold_render = True
        self._render_held = False
        self.logs = []

    def _call(self, **kw):   # pragma: no cover - the point: never reached
        raise AssertionError("the routine miner must never call a model")

    def _log(self, type, **fields):
        self.logs.append((type, fields))


def test_the_pass_through_the_portrait_worker_calls_no_model_and_asks_for_the_held_render(store, monkeypatch):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    monkeypatch.setattr(R, "load_skills", lambda: SKILLS)
    worker = _Worker(store)
    with worker._run_lock:   # as portrait._routines holds it
        res = R.RoutineMiner(worker).run(now=R.day_start(MON + timedelta(days=21)), reason="after the nightly run")
    assert res.ok and res.cost_usd == 0.0 and res.applied == 1 and res.run_id is not None
    assert worker._render_held is True
    [(kind, fields)] = worker.logs
    assert kind == "routines_mined" and fields["ok"] and fields["active"] == 1 and fields["offerable"] == 1
    assert fields["cpu_ms"] < 1000 and fields["keys"] == {"meet.open|zen": 15}


def test_the_portrait_schedulers_night_runs_the_code_miner(store, tmp_path, monkeypatch):
    from yuki.memory.portrait import PortraitScheduler, PortraitWorker

    today = R.day_of(time.time())
    first = today - timedelta(days=22)
    setup_presence(store, first, 23)
    for d in days(first, 22, weekdays=True):
        moment(store, at(d, 8, 58))
    monkeypatch.setattr(R, "load_skills", lambda: SKILLS)
    worker = PortraitWorker(store, client=object(), log_dir=tmp_path / "logs")
    monkeypatch.setattr(worker, "_call", lambda **kw: (_ for _ in ()).throw(AssertionError("no model call")))
    logs = []
    sched = PortraitScheduler(store, worker, db_path=tmp_path / "memory.db", log=lambda t, **f: logs.append((t, f)),
                              idle_fn=lambda: 0.0)
    with worker.one_render():
        sched._routines("nightly")
        assert worker._render_held is True
        worker._render_held = False   # the one render at the end of the night (a model call) is not this test's
    [(kind, fields)] = [x for x in logs if x[0] == "portrait_run"]
    assert fields["kind"] == "routines" and fields["ok"] and fields["changes"] == 1 and fields["cost_usd"] == 0.0
    mined = [json.loads(line) for line in (tmp_path / "logs").glob("portrait-*.jsonl").__next__().read_text(
        encoding="utf-8").splitlines()]
    assert [m["type"] for m in mined] == ["routines_mined"] and mined[0]["active"] == 1
    assert [r["state"] for r in store.routines()] == ["active"]


def test_the_log_and_the_report_are_content_free(store, tmp_path):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    R.tell(store, "meet.open", "zen", "weekdays", 11 * 60, label="Invented Standup Name", now=at(MON, 6))
    playlist = Skill("spotify.playlist", (("media_context", "spotify"),), True, "change", (Slot("name", True),))
    secret = "f" * 64
    for d in days(MON, 21):
        moment(store, at(d, 22, 5), app="spotify", kind="media_context", digest=secret, service=None)
    logs = []
    res = R.RoutineMiner(store=store, skills=SKILLS + (playlist,), log=lambda t, **f: logs.append((t, f))).run(
        now=R.day_start(MON + timedelta(days=21)))
    assert res.ok
    text = json.dumps(logs, default=str)
    assert "Invented Standup Name" not in text and "Google Meet" not in text and "Zen" not in text
    assert secret not in text and ("spotify.playlist|spotify|" + "f" * 12) in text
    rep = json.dumps(R.report(store, at=R.day_start(MON + timedelta(days=21))), default=str)
    assert "Invented Standup Name" not in rep and secret not in rep


def test_the_mcp_never_reads_the_routine_tables():
    src = (Path(__file__).resolve().parents[1] / "yuki" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "routine_events" not in src and "FROM routines" not in src and "store.routines(" not in src


def test_mining_28_busy_days_stays_under_a_second_of_cpu(store):
    rng = random.Random(11)
    setup_presence(store, MON, 29, spans=((7, 0, 13, 0), (14, 0, 17, 0), (18, 0, 23, 59)))
    for d in days(MON, 28):
        moment(store, at(d, 8, 58) + rng.uniform(-120, 120))
        for _ in range(4):   # an evening voice channel at all hours
            moment(store, at(d, rng.choice([18, 19, 20, 21, 22]), rng.randrange(60)), app="discord",
                   service="Discord")
    until = R.day_start(MON + timedelta(days=28))
    c0 = time.thread_time()
    inp = R.keyed_onsets(store, until - 28 * 86400, until, skills=SKILLS)
    got = R.mine(inp, until)
    cpu = time.thread_time() - c0
    assert cpu < 1.0, cpu
    assert any(w.key == "meet.open|zen" for w in got.passed)


# -- for the card lane and the talker -------------------------------------------------------------------------------


def _routine(store, *, at_min=538.0, dayset="weekdays", mode="early", key="meet.open|zen", skill="meet.open",
             app="zen", state="active"):
    return store.save_routine({"key": key, "skill_id": skill, "app": app, "trigger": "clock", "dayset": dayset,
                               "at_min": at_min, "state": state, "offer_mode": mode, "hits": 7, "eligible": 9,
                               "rate": 0.75, "created_at": 1.0, "updated_at": 1.0})


WED = MON + timedelta(days=2)


def test_due_offers_is_the_early_window_of_an_offerable_trusted_routine(store):
    rid = _routine(store)
    yes = lambda s: True   # noqa: E731
    assert R.due_offers(store, at(WED, 8, 49), skills=SKILLS, trusted=yes) == []          # before at - 8
    [o] = R.due_offers(store, at(WED, 8, 52), skills=SKILLS, trusted=yes)
    assert o["id"] == rid and o["mode"] == "early" and o["expires_at"] == at(WED, 9, 8)
    assert R.due_offers(store, at(WED, 8, 58), skills=SKILLS, trusted=yes) == []          # after at - 1
    assert R.due_offers(store, at(WED, 8, 52), skills=SKILLS, trusted=lambda s: False) == []
    assert R.due_offers(store, at(MON + timedelta(days=5), 8, 52), skills=SKILLS, trusted=yes) == []   # Saturday


def test_due_offers_never_twice_never_after_the_join_never_for_a_context_skill(store):
    rid = _routine(store)
    yes = lambda s: True   # noqa: E731
    moment(store, at(WED, 8, 40))                                                          # joined early today
    assert R.due_offers(store, at(WED, 8, 52), skills=SKILLS, trusted=yes) == []
    thu = WED + timedelta(days=1)
    store.add_routine_event(rid, "offered", at(thu, 8, 51))
    assert R.due_offers(store, at(thu, 8, 53), skills=SKILLS, trusted=yes) == []          # offered today
    vid = _routine(store, at_min=19 * 60, key="discord.voice|discord", skill="discord.voice", app="discord")
    assert R.due_offers(store, at(thu, 18, 55), skills=SKILLS, trusted=yes) == []         # not offerable
    assert vid


def test_due_offers_keeps_the_daily_cap_and_the_modes(store):
    yes = lambda s: True   # noqa: E731
    a = _routine(store, at_min=8 * 60)
    b = _routine(store, at_min=12 * 60)
    _routine(store, at_min=17 * 60)
    store.add_routine_event(a, "offered", at(WED, 7, 53))
    store.add_routine_event(b, "offered", at(WED, 11, 53))
    assert R.due_offers(store, at(WED, 16, 55), skills=SKILLS, trusted=yes) == []         # 2 a day
    late = _routine(store, at_min=540.0, mode="late")
    thu = WED + timedelta(days=1)
    assert [o["id"] for o in R.due_offers(store, at(thu, 9, 2), skills=SKILLS, trusted=yes)] == [late]
    store.set_routine_mode(late, "quiet")
    assert R.due_offers(store, at(thu, 9, 2), skills=SKILLS, trusted=yes) == []


def test_due_offers_asks_the_d14_trust_rule_by_default(store, monkeypatch):
    _routine(store)
    seen = []
    import yuki.skills.registry as registry

    monkeypatch.setattr(registry, "offer_ready", lambda st, skill, *a, **k: seen.append(skill.key) or False)
    assert R.due_offers(store, at(WED, 8, 52), skills=SKILLS) == [] and seen == ["meet.open"]


def test_active_near_and_the_why_line(store):
    rid = _routine(store)
    near = R.active_near(store, at(WED, 8, 30))
    assert [r["id"] for r in near] == [rid] and near[0]["at"] == "08:58" and near[0]["in_min"] == 28.0
    assert R.active_near(store, at(WED, 11, 0)) == []
    assert R.why_line(store, store.routine(rid), skills=SKILLS) == (
        "You've joined it around 08:58 on 7 of the last 9 weekdays.")


# -- the CLI ---------------------------------------------------------------------------------------------------------


def test_cli_show_reads_without_writing(tmp_path, capsys):
    path = tmp_path / "memory.db"
    s = Store.open(path)
    _routine(s)
    s.close()
    before = path.stat().st_mtime_ns
    assert R.main(["--show", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "meet.open|zen weekdays ~08:58 active" in out and "hits 7/9" in out
    assert path.stat().st_mtime_ns == before


def test_cli_show_before_migration_42(tmp_path, capsys):
    import sqlite3

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    assert R.main(["--show", "--db", str(path)]) == 0
    assert "no routines table yet" in capsys.readouterr().out


def test_cli_report_runs_on_a_copy_and_the_pass_runs_dry(tmp_path, capsys, monkeypatch):
    path = tmp_path / "memory.db"
    s = Store.open(path)
    setup_presence(s, MON, 28)
    e1(s, weeks=3)
    s.close()
    monkeypatch.setattr(R, "load_skills", lambda: SKILLS)
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    assert R.main(["--report", "--db", str(path), "--at", (MON + timedelta(days=21)).isoformat() + "T04:00"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["passed"][0]["key"] == "meet.open|zen" and rep["would_change"][0]["events"][0]["state_to"] == "active"
    s = Store.open(path)
    try:
        assert s.routines() == []                      # the report wrote nothing to the database it read
    finally:
        s.close()
    assert R.main(["--db", str(path), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["cost_usd"] == 0.0


# -- review round 1 ------------------------------------------------------------------------------------------------


def test_a_failing_window_never_uses_up_the_permutation_slots_of_another(tmp_path):
    """Review L1's probe: a drifting 09:00 window (fails stability) beside a clean 14:00 window of the same key."""
    def build(name, with_morning):
        s = Store.open(tmp_path / name)
        setup_presence(s, MON, 29, spans=((7, 0, 13, 0), (13, 30, 17, 0), (18, 0, 23, 30)))
        wk = days(MON, 28, weekdays=True)
        for i, d in enumerate(wk):
            if with_morning:   # first half ~08:45, second half ~09:05: rate, spread and lift pass; stability fails
                base = 45 if i < len(wk) // 2 else 65
                moment(s, at(d, 8, 0) + 60 * (base + (i % 3) - 1))
            if i % 4 != 3:     # 14:00 on 3 of 4 weekdays, tight
                moment(s, at(d, 14, 0) + 60 * ((i % 3) - 1))
        return s

    for name, morning in (("alone.db", False), ("both.db", True)):
        s = build(name, morning)
        try:
            got = _mine(s, MON + timedelta(days=28))
            assert [(R.hm(w.at_min)[:2], w.dayset) for w in got.passed] == [("14", "weekdays")], name
            assert got.passed[0].hits == 15
        finally:
            s.close()


def test_n_of_m_never_counts_a_hit_on_a_day_that_was_not_eligible(store):
    setup_presence(store, MON, 28)
    e1(store, weeks=3)
    tue = MON + timedelta(days=15)
    # a hit day with the user away around the window's centre (a join on another device): a hit for the bars, but
    # not an eligible day, so the line never says more hits than days
    store._write(lambda c: c.execute("DELETE FROM timeline WHERE started_at >= ? AND started_at < ?",
                                     (R.day_start(tue), R.day_start(tue + timedelta(days=1)))))
    present(store, tue, spans=((9, 40, 13, 0),))
    got = _mine(store, MON + timedelta(days=21))
    [w] = got.passed
    assert w.hits == 15 and w.eligible == 14 and w.elig_hits == 14
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21))
    [row] = store.routines()
    assert (row["hits"], row["eligible"]) == (14, 14)
    [f] = store.portrait_facts(("routine",))
    assert f.text.startswith("On 14 of the last 14 weekdays")
    assert R.why_line(store, row, skills=SKILLS).endswith("on 14 of the last 14 weekdays.")


def test_a_routine_whose_best_centre_wobbles_stays_active(store):
    """Review L3: becoming active takes every bar; staying active only the rate and the misses near its time. Two
    tight weeks at ~19:20, then a month of joins 15-55 min earlier: some nights the best window fails a bar (spread,
    stability) while the stored centre has lost hits; the routine still holds near its time, so it never flaps (the
    decay at the stored centre alone went dormant and back three nights running on these rows)."""
    setup_presence(store, MON, 56)
    rng = random.Random(1)
    for d in days(MON, 14, weekdays=True):
        moment(store, at(d, 19, 20) + rng.uniform(-4, 4) * 60, app="discord", service="Discord")
    for d in days(MON + timedelta(days=14), 28, weekdays=True):
        moment(store, at(d, 19, 20) + rng.uniform(-55, -15) * 60, app="discord", service="Discord")
    voice = Skill("discord.voice", (("call_join", "discord"),), True, "change")
    states = []
    for i in range(14, 42):
        res = R.RoutineMiner(store=store, skills=(MEET, voice)).run(now=R.day_start(MON + timedelta(days=i)))
        assert res.ok, res.error
        states.append(tuple(r["state"] for r in store.routines()))
    assert set(states) == {("active",)}, states
    [row] = store.routines()
    assert [e["detail"]["state_to"] for e in store.routine_events(row["id"], kinds=["state"])] == ["active"]


def test_active_near_leaves_out_context_routines(store):
    rid = _routine(store, at_min=8 * 60 + 40)
    _routine(store, at_min=8 * 60 + 50, key="discord.voice|discord", skill="discord.voice", app="discord")
    near = R.active_near(store, at(WED, 8, 30), skills=SKILLS)
    assert [r["id"] for r in near] == [rid]


def test_due_offers_never_returns_more_than_the_cards_left_today(store):
    yes = lambda s: True   # noqa: E731
    a = _routine(store, at_min=8 * 60)
    _routine(store, at_min=12 * 60, key="meet.open|zen|x", app="zen")
    _routine(store, at_min=12 * 60 + 1, key="meet.open|zen|y", app="zen")
    store.add_routine_event(a, "offered", at(WED, 7, 53))
    store._write(lambda c: c.execute("DELETE FROM moments"))
    got = R.due_offers(store, at(WED, 11, 54), skills=SKILLS, trusted=yes)
    assert len(got) == 1   # 1 offered today, 2 due at once: 1 card left


def test_t6_a_moved_routine_offers_at_most_twice_then_the_new_time(store):
    """Appendix E's T6 on invented rows shaped like E1 (7 hits on the 10 weekdays before a Friday, then the meeting
    moves +60 min), through due_offers as the lane calls it (every minute, one card per routine a day, each offer
    recorded): the old window offers at most twice more and never once it went dormant; the new window is active the
    night after its 5th hit day and offered from then on."""
    setup_presence(store, MON, 70, spans=((7, 0, 13, 0), (14, 0, 17, 0), (18, 0, 23, 59)))
    shift = MON + timedelta(days=25)                                                  # a Friday
    old_days = [d for d in days(MON + timedelta(days=11), 14, weekdays=True) if d < shift]
    assert len(old_days) == 10
    for i, d in enumerate(old_days):
        if i not in (3, 7, 8):
            moment(store, at(d, 8, 58) + 60 * (0, 2, -1, 1, -2)[i % 5])
    new_days = days(shift, 28, weekdays=True)
    for i, d in enumerate(new_days):
        moment(store, at(d, 9, 58) + 60 * (0, 2, -1, 1, -2)[i % 5])
    yes = lambda s: True   # noqa: E731
    offers = []
    for i in range(11, 49):
        d = MON + timedelta(days=i)
        assert miner(store).run(now=R.day_start(d)).ok
        t = at(d, 8, 0)
        while t <= at(d, 10, 30):
            for o in R.due_offers(store, t, skills=SKILLS, trusted=yes):
                store.add_routine_event(o["id"], "offered", t)
                offers.append((d, o["id"]))
            t += 60
    rows = store.routines()
    [old] = [r for r in rows if abs(r["at_min"] - 538) < 30]
    [new] = [r for r in rows if abs(r["at_min"] - 598) < 30]
    false = [d for d, rid in offers if rid == old["id"] and d >= shift]
    dormant = [R.day_of(e["at"]) for e in store.routine_events(old["id"], kinds=["state"])
               if e["detail"]["state_to"] == "dormant"]
    assert [d for d, rid in offers if rid == old["id"] and d < shift]   # it was offered before the move
    after = [d for d in dormant if d > shift]
    assert len(false) <= 2 and after and all(d < after[0] for d in false)
    assert not [e for e in store.routine_events(old["id"], kinds=["state"])
                if e["detail"]["state_to"] == "active" and R.day_of(e["at"]) > after[0]]   # and stays down
    became = [R.day_of(e["at"]) for e in store.routine_events(new["id"], kinds=["state"])
              if e["detail"]["state_to"] == "active"]
    assert became[0] == new_days[4] + timedelta(days=1)                   # the night after its 5th hit day
    new_offers = [d for d, rid in offers if rid == new["id"]]
    assert new_offers[0] == became[0] and new["state"] == "active"
    assert all(d in new_offers for d in new_days if became[0] <= d < MON + timedelta(days=49))
    assert len(offers) == len(set(offers))                                # one card per routine a day


def test_a_long_held_routine_that_stopped_fails_the_bars_after_five_eligible_misses(store):
    """R1.6 for a routine that still passes on its old hits: 15 weekday joins, then none. Before the 5th eligible
    miss it still passes (a hit rate of 0.67 from the old hits); at 5 misses in a row it fails ('misses') and the row
    goes dormant that night, even though the rate alone would keep it for days."""
    setup_presence(store, MON, 35)
    e1(store, weeks=3)
    assert _mine(store, MON + timedelta(days=21 + 4)).passed            # Friday 04:00: 4 misses (Mon-Thu)
    got = _mine(store, MON + timedelta(days=21 + 5))                     # Saturday: the 5th (Friday)
    assert got.passed == [] and "misses" in got.near["meet.open|zen"].failed
    assert got.near["meet.open|zen"].rate >= R.MIN_RATE
    run_days(store, MON + timedelta(days=21), MON + timedelta(days=21 + 5))
    [row] = store.routines()
    [ev] = [e for e in store.routine_events(row["id"], kinds=["state"]) if e["detail"]["state_to"] == "dormant"]
    assert R.day_of(ev["at"]) == MON + timedelta(days=21 + 5) and ev["detail"]["why"] == "misses_in_row"
