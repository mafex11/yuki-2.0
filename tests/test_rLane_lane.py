"""The routine lane (yuki.memory.routine_offers; PLAN.md Phase R, D10, D11, D14; B.3 lane tests): the "Your usual"
card in its usual window through the coach's real gates, held by itself on a game or full screen in front, the return
hold, any open call, meeting audio, away and Yuki acting; never twice a day, at most 2 a day, expiring when the join
lands; the mode ladder; offer-now through the same gates. Temp databases, an invented weekday, no model (a client
that fails the test if called); D14's trust and the Meet tab census are stubbed and said so.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

import pytest

from _rlane_world import (DAY, Clock, NoModel, add_routine, at, card, days_later, held, join, lane_worker,
                          open_store, present, routine_cards, run_ticks)
from yuki.memory import offers, routine_offers
from yuki.memory.nudges import NudgeConfig
from yuki.memory.store import TimelineRow


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = open_store(tmp)
        try:
            yield s
        finally:
            s.close()


def sat_down(store, start: str, end: str, day=DAY, **kw) -> None:
    present(store, at(start, day), at(end, day), **kw)


# -- fires in its window, through settling, with its words ---------------------------------------------------------


def test_the_card_comes_in_the_usual_window_through_settling_with_its_words(store):
    rid = add_routine(store)
    sat_down(store, "08:51:00", "08:53:00")          # sat down at 08:51: the coach's gate is "settling" until 08:56
    clock = Clock(at("08:51:40"))
    w = lane_worker(store, clock)
    assert run_ticks(w, clock, at("08:50:00"), at("08:51:59")) == []    # before the window (08:52-08:59)
    got = run_ticks(w, clock, at("08:52:00"), at("08:52:00"))
    assert len(got) == 1 and got[0]["routine"] == rid and got[0]["lead_s"] == 480
    n = routine_cards(store)[0]
    assert n.kind == "notice" and n.trigger == "routine"
    assert n.text == "Your 9:00 meeting is coming up. Open the Meet tab in Zen?"
    assert n.reason == "You've joined it around 09:00 on 7 of the last 9 weekdays."
    offer = n.inputs["offer"]
    assert offer["do"] == "run_skill" and offer["skill"] == "meet.open" and offer["refs"] == [f"R{rid}"]
    assert offer["label"] == "Open it" and offer["steps"] == ["Open the Meet tab in Zen"]
    assert offer["wont"] == "Won't join, unmute, type or send anything. You click Join."
    assert offer["expires_at"] == pytest.approx(at("09:10"))
    view = offers.card_offer_view(n)
    assert view["why"] == n.reason and view["undo"] is True
    kinds = [e["kind"] for e in routine_offers.events(store, routine_id=rid)]
    assert kinds == ["offered"]
    assert any(t == "routine_offer" and f["routine"] == rid for t, f in w.logged)


def test_the_why_line_uses_the_measured_time_and_a_told_name(store):
    add_routine(store, at_min=538.0, hits=5, eligible=7, label="Daily Blocker Sync")
    sat_down(store, "08:40", "08:53")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    run_ticks(w, clock, at("08:50"), at("08:52"))
    n = routine_cards(store)[0]
    assert n.text == "Daily Blocker Sync is at 9:00. Open the Meet tab in Zen?"
    assert n.reason == "You've joined it around 08:58 on 5 of the last 7 weekdays."


def test_no_card_outside_its_days_or_after_its_window(store):
    add_routine(store)
    saturday = days_later(3)
    sat_down(store, "08:30", "09:30", day=saturday)
    clock = Clock(at("08:52", saturday))
    w = lane_worker(store, clock)
    assert run_ticks(w, clock, at("08:30", saturday), at("09:30", saturday), step=30) == []
    sat_down(store, "09:00", "09:30")
    clock.t = at("09:00:30")
    assert run_ticks(w, clock, at("09:00:30"), at("09:20"), step=30) == []   # the window ended at 08:59


# -- D11: held by itself, whatever the gate says ---------------------------------------------------------------------


@pytest.mark.parametrize("live,reason", [
    ({"game_front": True}, "game"),
    ({"fullscreen": True}, "fullscreen"),
    ({"acting": True}, "yuki_acting"),
    ({"meeting": True, "game_front": True}, "game"),     # gate "meeting" with a game in front: still held
])
def test_held_on_a_game_full_screen_or_yuki_acting(store, live, reason):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock, live=live)
    assert run_ticks(w, clock, at("08:52"), at("08:59")) == []
    assert reason in held(w) and routine_cards(store) == []
    assert [e["detail"]["reason"] for e in routine_offers.events(store, kinds=("held",))] == [reason]   # once a day


def test_held_for_the_return_hold_after_a_game_then_shown(store):
    add_routine(store)
    sat_down(store, "08:30", "08:51")
    store.save_timeline(TimelineRow(None, at("08:51"), at("08:52:30"), "present", "valorant-win64-shipping",
                                    "VALORANT", active_s=90, fullscreen=True))
    sat_down(store, "08:52:30", "09:00")
    clock = Clock(at("08:52:45"))
    w = lane_worker(store, clock, live={"game_front": False})
    got = run_ticks(w, clock, at("08:52:45"), at("08:59"))
    assert "return_hold" in held(w)
    assert len(got) == 1 and clock.t >= at("08:54:30")   # 120 s after the game left the front


def test_held_while_a_call_is_open_even_behind_another_window_with_gate_none(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    store.start_call(app="Discord", process="Discord.exe", via="mic", at=at("08:40"), status="recording")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    assert run_ticks(w, clock, at("08:52"), at("08:59")) == []
    assert "call" in held(w)


def test_a_games_voice_chat_with_the_game_behind_is_no_call(store):
    """8 Oct 08:51: VALORANT's voice session had been open behind Zen since 07:01; the card may come (D8, R2.3)."""
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    store.start_call(app="VALORANT", process="VALORANT-Win64-Shipping.exe", via="game", at=at("07:01"),
                     status="recording", kind="game")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock, live={"game_front": False})
    assert len(run_ticks(w, clock, at("08:52"), at("08:52"))) == 1 and "call" not in held(w)


def test_held_while_the_last_meetings_app_still_plays(store):
    add_routine(store, at_min=600.0)   # 10:00
    sat_down(store, "09:00", "09:40")
    # a meeting row of this very process ended 10 min ago; its app still renders sound (Meet muted the mic)
    store.save_timeline(TimelineRow(None, at("09:40"), at("09:42"), "present", "python", "Python", active_s=120,
                                    meeting="Google Meet"))
    sat_down(store, "09:47", "10:00")
    clock = Clock(at("09:52:30"))
    w = lane_worker(store, clock, render=[{"state": "active", "pid": os.getpid()}])
    assert run_ticks(w, clock, at("09:52:30"), at("09:59")) == []
    assert "meeting_audio" in held(w)


def test_held_when_away_and_under_the_coarse_gates(store):
    add_routine(store)
    sat_down(store, "08:00", "08:40")   # left at 08:40: away by 08:52
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    assert run_ticks(w, clock, at("08:52"), at("08:59")) == [] and "away" in held(w)
    sat_down(store, "08:40", "09:00")
    quiet = NudgeConfig.from_dict({"presence_enabled": False, "checkins_enabled": False, "questions_enabled": False,
                                   "resume_enabled": False, "quiet_start": "08:00", "quiet_end": "09:30"})
    w = lane_worker(store, Clock(at("08:52")), config=quiet)
    assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == []
    w = lane_worker(store, Clock(at("08:52")), locked=True)
    assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == []
    store.set_nudge_state("quiet_until", str(at("09:30")))
    w = lane_worker(store, Clock(at("08:52")))
    assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == []
    assert routine_cards(store) == []


def test_offers_off_or_the_lane_off_shows_nothing(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    for table in ({"offers": "off"}, {"routine_offers": False}):
        cfg = NudgeConfig.from_dict({"presence_enabled": False, "checkins_enabled": False,
                                     "questions_enabled": False, "resume_enabled": False, **table})
        w = lane_worker(store, Clock(at("08:52")), config=cfg)
        assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == []
    assert routine_cards(store) == []


# -- the shared gates: the card gap is skipped, a card showing never jumped ----------------------------------------


def test_it_skips_the_card_gap_but_never_jumps_a_card_showing(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    card(store, at("08:48"), trigger="resume", kind="resume", shown=True, reaction="shown")   # faded at 08:48
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    assert len(run_ticks(w, clock, at("08:52"), at("08:52"))) == 1          # 4 min after another card: still shown
    other = routine_offers.routine(store, add_routine(store, at_min=545.0, key="meet.open|chrome"))   # 09:05
    card(store, at("08:57"), trigger="thread", shown=True)                  # on screen now, nobody reacted
    assert run_ticks(w, clock, at("08:57:30"), at("09:00")) == []
    assert "attention:card_pending" in held(w) and other is not None


# -- once a day, two a day at most, never when it happened already ---------------------------------------------------


def test_never_twice_a_day_and_never_more_than_two(store):
    a = add_routine(store, at_min=540.0)
    b = add_routine(store, at_min=660.0, key="meet.open|chrome")      # 11:00
    c = add_routine(store, at_min=840.0, key="meet.open|arc")         # 14:00
    sat_down(store, "08:30", "14:30")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    got = run_ticks(w, clock, at("08:52"), at("08:59"))
    assert [g["routine"] for g in got] == [a]
    store.react_to_nudge(got[0]["nudge_id"], "dismissed", at=at("08:53"))
    assert run_ticks(w, clock, at("08:54"), at("08:59")) == []           # Not now: not again today
    got = run_ticks(w, clock, at("10:52"), at("10:59"))
    assert [g["routine"] for g in got] == [b]
    assert run_ticks(w, clock, at("13:52"), at("13:59")) == []           # the day's two are spent
    assert len(routine_cards(store)) == 2 and c


def test_not_offered_once_the_user_joined_or_said_open_my_meeting(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    join(store, at("08:50"))
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    assert run_ticks(w, clock, at("08:52"), at("08:59")) == []
    nxt = days_later(1)
    sat_down(store, "08:30", "09:00", day=nxt)
    join(store, at("08:51", nxt), by="yuki")   # Yuki's own run's moment never counts as the user's (D18) ...
    with store._lock:                          # ... but a voice "open my meeting" that ended done does
        store._conn.execute("INSERT INTO skill_runs(at, skill, origin, t0, t_done, outcome) VALUES (?,?,?,?,?,?)",
                            (at("08:51", nxt), "meet.open", "voice", at("08:51", nxt), at("08:51:02", nxt), "done"))
    clock.t = at("08:52", nxt)
    assert run_ticks(w, clock, at("08:52", nxt), at("08:59", nxt)) == []
    later = days_later(2)
    sat_down(store, "08:30", "09:00", day=later)
    join(store, at("08:51", later), by="yuki")
    clock.t = at("08:52", later)
    assert len(run_ticks(w, clock, at("08:52", later), at("08:52", later))) == 1


def test_the_card_expires_when_the_join_lands(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    nid = run_ticks(w, clock, at("08:52"), at("08:52"))[0]["nudge_id"]
    join(store, at("08:55"))
    run_ticks(w, clock, at("08:56"), at("08:56"))
    assert store.nudge(nid).outcome == "expired"
    assert offers.act(store, nid, now=at("08:57"))["why"] == "already"   # its button runs nothing now


# -- trust and availability (D14, D8) ---------------------------------------------------------------------------------


def test_no_card_without_a_trusted_route_or_a_meet_tab(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    w = lane_worker(store, Clock(at("08:52")), ready=False)
    assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == [] and "not_ready" in held(w)
    w = lane_worker(store, Clock(at("08:52")), avail=False)
    assert run_ticks(w, w.clock, at("08:52"), at("08:59")) == [] and "unavailable" in held(w)
    assert w.availability_calls == ["meet.open"]                        # the census at most once a day
    w = lane_worker(store, Clock(at("08:52")), avail=None)              # an unreadable strip: no card either
    assert run_ticks(w, w.clock, at("08:52"), at("08:53")) == []
    assert routine_cards(store) == []


def test_the_real_registry_holds_it_until_two_verified_runs(store):
    from yuki.skills import registry
    from yuki.skills.connection import load

    add_routine(store)
    sat_down(store, "08:30", "09:00")
    w = lane_worker(store, Clock(at("08:52")))
    w.routines = routine_offers.RoutineLane(w, available=lambda s, k: True)    # D14's real offer_ready
    assert run_ticks(w, w.clock, at("08:52"), at("08:53")) == [] and "not_ready" in held(w)
    skill = load().get("meet.open")
    registry.seed(store, load())
    for k in range(2):
        key = registry.start_run(store, skill=skill, origin="voice", t0=at("08:00") - 86400 * (k + 1),
                                 run_key=registry.new_run_key(), request_id=None, session_id=None, filled={})
        route = skill.routes[0]
        registry.finish_run(store, key, outcome="done", checked=True, route=route, ms=900.0)
    assert registry.offer_ready(store, skill)
    w.routines = routine_offers.RoutineLane(w, available=lambda s, k: True)
    assert len(run_ticks(w, w.clock, at("08:54"), at("08:54"))) == 1


# -- learning: the mode ladder (B.3) ---------------------------------------------------------------------------------


def _day_card(store, w, clock, day, *, start="08:52", end="08:59", reaction=None, outcome=None) -> int | None:
    sat_down(store, "08:30", "09:20", day=day)
    got = run_ticks(w, clock, at(start, day), at(end, day))
    if not got:
        return None
    nid = got[0]["nudge_id"]
    if reaction:
        store.react_to_nudge(nid, reaction, at=at(start, day) + 30)
    if outcome:
        offers._set_outcome(store, nid, outcome, at=at(start, day) + 40)
    return nid


def _settle(w, clock, day) -> None:
    clock.t = at("09:30", day)
    w.routines._learned_at = 0.0
    w.routines.learn(clock.t, w.config())


def test_less_of_these_steps_early_late_quiet_then_off(store):
    rid = add_routine(store)
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    nid = _day_card(store, w, clock, DAY, reaction="less")
    assert routine_offers.feedback(store, nid, "less", now=at("08:53"))["mode_to"] == "late"
    assert routine_offers.feedback(store, nid, "less", now=at("08:54"))["why"] == "already"   # once per card
    _settle(w, clock, DAY)
    assert routine_offers.routine(store, rid).offer_mode == "late"     # the learning pass didn't step it again
    thu = days_later(1)
    assert _day_card(store, w, clock, thu, start="08:52", end="09:00:30") is None   # no early card any more
    nid = _day_card(store, w, clock, thu, start="09:01", end="09:03")
    assert routine_cards(store)[-1].text == "Your 9:00 meeting started a minute ago. Open the Meet tab in Zen?"
    store.react_to_nudge(nid, "less", at=at("09:02", thu))
    _settle(w, clock, thu)                                            # the reaction alone steps it (any surface)
    assert routine_offers.routine(store, rid).offer_mode == "quiet"
    assert _day_card(store, w, clock, days_later(2), start="08:52", end="09:05") is None   # quiet: no card
    assert routine_offers.feedback(store, nid, "never", now=at("10:00", thu))["mode_to"] == "off"
    kinds = [e["kind"] for e in routine_offers.events(store, routine_id=rid) if e["kind"] in ("mode_change", "never")]
    assert kinds == ["mode_change", "mode_change", "never"]


def test_joining_without_it_three_times_moves_it_late(store):
    rid = add_routine(store)
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    for k in range(3):
        day = days_later(k if k < 3 else k + 2)
        nid = _day_card(store, w, clock, day)
        assert nid is not None
        join(store, at("08:56", day))                                 # walked to Zen themselves
        _settle(w, clock, day)
    kinds = [e["kind"] for e in routine_offers.events(store, routine_id=rid)]
    assert kinds.count("self_served") == 3 and kinds.count("ignored") == 3
    assert routine_offers.routine(store, rid).offer_mode == "late"


def test_ignoring_the_late_card_three_times_makes_it_quiet(store):
    rid = add_routine(store, mode="late")
    clock = Clock(at("09:01"))
    w = lane_worker(store, clock)
    for k in (0, 1, 2):
        day = days_later(k)
        nid = _day_card(store, w, clock, day, start="09:01", end="09:03")
        assert nid is not None
        store.react_to_nudge(nid, "shown", at=at("09:01:30", day))   # seen, faded, no reaction
        _settle(w, clock, day)
    assert routine_offers.routine(store, rid).offer_mode == "quiet"


def test_acceptance_under_30_percent_after_ten_meeting_days_makes_it_quiet(store):
    rid = add_routine(store)
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    weekdays = [d for d in (days_later(k) for k in range(16)) if d.weekday() < 5][:10]
    for i, day in enumerate(weekdays):
        nid = _day_card(store, w, clock, day, reaction=None if i < 9 else "dismissed",
                        outcome="done" if i in (0, 1) else None)
        assert nid is not None, day
        if i not in (0, 1):
            store.react_to_nudge(nid, "dismissed", at=at("08:53", day))   # Not now (never self-served: no join)
        join(store, at("09:10", day))   # the meeting happened every day (late: not "joined without the card")
        _settle(w, clock, day)
        mode = routine_offers.routine(store, rid).offer_mode
        if i < 9:
            assert mode == "early", (i, mode)
    days, seen, accepted = w.routines.acceptance(rid, at("09:30", weekdays[-1]), w.config())
    assert (days, seen, accepted) == (10, 10, 2)
    assert routine_offers.routine(store, rid).offer_mode == "quiet"


def test_a_card_the_screen_never_showed_is_not_counted_as_ignored(store):
    rid = add_routine(store, mode="late")
    clock = Clock(at("09:01"))
    w = lane_worker(store, clock)
    for k in (0, 1, 2):
        day = days_later(k)
        assert _day_card(store, w, clock, day, start="09:01", end="09:03") is not None
        _settle(w, clock, day)
    evs = routine_offers.events(store, routine_id=rid, kinds=("ignored",))
    assert len(evs) == 3 and all(e["detail"]["seen"] is False for e in evs)
    assert routine_offers.routine(store, rid).offer_mode == "late"


def test_an_accepted_card_with_the_join_after_it_is_joined(store):
    rid = add_routine(store)
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    nid = _day_card(store, w, clock, DAY)
    store.react_to_nudge(nid, "accepted", at=at("08:53"))
    assert offers._set_outcome(store, nid, "running", at=at("08:53"), only_if=(None,))
    assert offers.offer_result(store, nid, True, {"outcome": "done", "route": "a:census:driver", "ms": 812.0},
                               now=at("08:53:02"))
    join(store, at("08:56"))
    _settle(w, clock, DAY)
    ev = {e["kind"]: e for e in routine_offers.events(store, routine_id=rid)}
    assert ev["accepted"]["detail"] == {"mode": "early", "run": "done", "joined": True}
    assert ev["run_ok"]["detail"] == {"outcome": "done", "route": "a:census:driver", "ms": 812.0}
    assert "self_served" not in ev


# -- offer-now: a request file, the real card gates -----------------------------------------------------------------


def test_offer_now_goes_through_the_real_gates(store):
    rid = add_routine(store)
    sat_down(store, "14:00", "14:30")
    clock = Clock(at("14:10"))
    live = {"game_front": False}
    w = lane_worker(store, clock, live=live)
    path = routine_offers.request_offer_now(store.path, rid, in_s=30, now=at("14:10"))
    assert path.parent == store.path.parent
    assert run_ticks(w, clock, at("14:10"), at("14:10:15")) == []      # its time hasn't come
    live["game_front"] = True                                          # switched to VALORANT within the 30 s
    assert run_ticks(w, clock, at("14:10:45"), at("14:12")) == []
    assert routine_offers.read_request(store.path)["state"] == "pending"
    assert routine_offers.read_request(store.path)["reason"] == "game"
    live["game_front"] = False                                         # VALORANT behind (return hold first)
    got = run_ticks(w, clock, at("14:12:15"), at("14:16"))
    assert len(got) == 1 and got[0]["forced"] is True
    req = routine_offers.read_request(store.path)
    assert req["state"] == "shown" and req["nudge_id"] == got[0]["nudge_id"]
    n = store.nudge(got[0]["nudge_id"])
    assert n.inputs["offer"]["expires_at"] == pytest.approx(n.at + 600)


def test_offer_now_for_an_unknown_or_quiet_routine_and_its_expiry(store):
    sat_down(store, "14:00", "15:00")
    clock = Clock(at("14:10"))
    w = lane_worker(store, clock)
    routine_offers.request_offer_now(store.path, 999, now=at("14:10"))
    run_ticks(w, clock, at("14:10"), at("14:10"))
    assert routine_offers.read_request(store.path)["state"] == "unknown_routine"
    rid = add_routine(store, mode="quiet")
    routine_offers.request_offer_now(store.path, rid, now=at("14:11"))
    run_ticks(w, clock, at("14:11"), at("14:11"))
    assert routine_offers.read_request(store.path)["state"].startswith("not_offering")
    untrusted = add_routine(store, key="meet.open|edge")
    w2 = lane_worker(store, clock, ready=False)
    routine_offers.request_offer_now(store.path, untrusted, now=at("14:11:30"))
    run_ticks(w2, clock, at("14:11:30"), at("14:11:45"))
    assert routine_offers.read_request(store.path)["state"] == "not_ready"   # said at once, never retried
    assert w2.availability_calls == []
    rid = add_routine(store, key="meet.open|chrome")
    store.start_call(app="Discord", process="Discord.exe", via="mic", at=at("14:00"), status="recording")
    routine_offers.request_offer_now(store.path, rid, now=at("14:12"))
    run_ticks(w, clock, at("14:12"), at("14:23"), step=60)
    req = routine_offers.read_request(store.path)
    assert req["state"] == "expired" and req["reason"] == "call" and routine_cards(store) == []


def test_offer_now_cli_writes_the_request_only(store, capsys):
    rid = add_routine(store)
    assert routine_offers.main(["offer-now", str(rid), "--in", "30", "--db", str(store.path)]) == 0
    req = routine_offers.read_request(store.path)
    assert req["routine_id"] == rid and req["state"] == "pending" and req["at"] - req["made_at"] == pytest.approx(30)
    assert routine_offers.main(["status", "--db", str(store.path)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["state"] == "pending"


# -- footprint and no model ------------------------------------------------------------------------------------------


def test_without_the_routine_tables_the_lane_does_nothing_and_starts_no_thread():
    with tempfile.TemporaryDirectory() as tmp:
        s = open_store(tmp, tables=False)
        try:
            sat_down(s, "08:30", "09:00")
            clock = Clock(at("08:52"))
            w = lane_worker(s, clock)
            before = threading.active_count()
            t0 = time.perf_counter()
            assert run_ticks(w, clock, at("08:52"), at("08:59")) == []
            assert threading.active_count() == before
            assert (time.perf_counter() - t0) < 5.0
        finally:
            s.close()


def test_a_tick_with_an_active_routine_outside_its_window_reads_no_card(store, monkeypatch):
    add_routine(store)
    sat_down(store, "10:00", "10:30")
    clock = Clock(at("10:20"))
    w = lane_worker(store, clock)
    calls = []
    original = w.routines._today_cards
    monkeypatch.setattr(w.routines, "_today_cards", lambda *a: calls.append(1) or original(*a))
    w.routines._learned_at = clock.t     # the learning pass aside
    w.routines.tick(clock.t, w.config(), None, "quiet_hours")
    run_ticks(w, clock, at("10:20"), at("10:21"))
    assert calls == []


def test_the_lane_never_calls_a_model(store):
    add_routine(store)
    sat_down(store, "08:30", "09:00")
    clock = Clock(at("08:52"))
    w = lane_worker(store, clock)
    assert isinstance(w.client, NoModel)
    assert len(run_ticks(w, clock, at("08:52"), at("08:59"))) == 1
    checkin = store.nudge_checkins(at("08:00"))[-1]
    assert checkin["trigger"] == "routine" and checkin["model"] is None and not checkin["cost_usd"]
