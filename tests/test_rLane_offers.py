"""The code-only ``run_skill`` offer (yuki.memory.offers; PLAN.md Phase R, D14, B.3 ``test_offers``): no model can
ever emit it (not in any writer's schema or the judge's enum, refused from any model dict, given to the routine
channel only), only an offerable, never-sending skill with a trusted route is offered, ``act`` answers ``running`` and
runs nothing, ``offer_result`` moves only ``running``, Undo works; the policy text keeps the work ban word for word.
Temp databases only; nothing touches the desktop or a model.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from _rlane_world import add_routine, open_store
from yuki.memory import offers, routine_offers
from yuki.memory.api import MemoryClient
from yuki.memory.store import NewNudge, NudgeCheckin

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = open_store(tmp)
        try:
            yield s
        finally:
            s.close()


def routine_card(store, rid: int, *, expires_in: float = 600.0, at: float | None = None, **over) -> int:
    at = time.time() if at is None else at
    r = routine_offers.routine(store, rid)
    offer, why = offers.routine_offer(store, r, at, expires_at=at + expires_in, why="You've joined it around 08:58.",
                                      ready=lambda s, k: True)
    assert offer is not None, why
    offer.update(over)
    _cid, nid = store.record_checkin(NudgeCheckin(at=at, trigger="routine", outcome="notice", model=None),
                                     NewNudge(kind="notice", text="Your 9:00 meeting is coming up.", trigger="routine",
                                              inputs={"offer": offer, "routine_id": rid, "mode": "early"}))
    return nid


# -- no model can ever emit run_skill ---------------------------------------------------------------------------------


def _schemas() -> list[dict]:
    from yuki.memory import checkins, nudges, presence

    found = []
    for mod in (nudges, presence, checkins, offers):
        for name in dir(mod):
            value = getattr(mod, name)
            if isinstance(value, dict) and (name.endswith("_TOOL") or name.endswith("_SCHEMA")
                                            or name.endswith("_PROPERTIES")):
                found.append(value)
    return found


def test_run_skill_is_in_no_model_schema_or_judge_enum():
    assert "run_skill" in offers.OFFER_DOS and offers.CODE_ONLY_DOS == ("run_skill",)
    assert "run_skill" not in offers.MODEL_DOS
    assert offers.OFFER_SCHEMA["anyOf"][1]["properties"]["do"]["enum"] == list(offers.MODEL_DOS)
    assert offers.JUDGE_OFFER_PROPERTIES["offer_do"]["enum"] == ["none", *offers.MODEL_DOS]
    schemas = _schemas()
    assert len(schemas) >= 6   # the coach's, the writers', the welcome back's, the judge's, the check-ins'
    for schema in schemas:
        assert "run_skill" not in json.dumps(schema), schema.get("name")


def test_allowed_dos_never_gives_it_to_a_model_written_card(store):
    for channel in ("checkin", "resume", "nudge", "notice", "stuck", "recap", "followup", "recall", "thread",
                    "plans"):
        got = offers.allowed_dos(store, channel)
        assert "run_skill" not in got and got == offers.MODEL_DOS, channel
    assert offers.allowed_dos(store, "routine") == ("run_skill",)
    assert offers.allowed_dos(store, "routine", enabled=False) == ()


def test_a_model_dict_with_run_skill_is_refused_whatever_it_claims(store):
    raw = {"do": "run_skill", "label": "Open it", "steps": ["open the Meet tab"], "refs": ["R1"], "text": "",
           "due": "", "skill": "meet.open"}
    for allowed in (None, ("run_skill",), offers.OFFER_DOS):
        assert offers.validate_offer(raw, ids={"R1"}, store=store, allowed_do=allowed) == (None, "not_allowed")


def test_the_checkin_resume_and_presence_paths_ask_for_their_own_channel():
    import inspect

    from yuki.memory import checkins, nudges, presence

    # every model-written card's allowed kinds come from allowed_dos on its own (non-routine) channel
    assert 'allowed_dos(self.store, "checkin"' in inspect.getsource(checkins)
    assert 'allowed_dos(self.store, "resume"' in inspect.getsource(nudges)
    assert 'allowed_dos(self.store, "nudge"' in inspect.getsource(nudges)
    assert "allowed_dos(self.store, look.channel" in inspect.getsource(presence)
    from yuki.memory.presence import CHANNELS

    assert "routine" not in CHANNELS


# -- the routine offer: written by code, only for an offerable skill with a trusted route --------------------------


def _skill(**over):
    base = dict(key="meet.open", offerable=True, sends=False, sends_when=(), preview="Open the Meet tab in Zen?",
                wont=("join",), slots=(SimpleNamespace(name="pick"), SimpleNamespace(name="link")))
    base.update(over)
    return SimpleNamespace(**base)


def _routine_offer(store, rid, skill, **kw):
    loaded = SimpleNamespace(get=lambda key: skill if key == skill.key else None)
    return offers.routine_offer(store, routine_offers.routine(store, rid), expires_at=time.time() + 600,
                                why="why", skills=lambda: loaded, **kw)


def test_only_an_offerable_never_sending_skill_with_a_trusted_route_is_offered(store):
    rid = add_routine(store)
    yes = lambda s, k: True   # noqa: E731
    assert _routine_offer(store, rid, _skill(), ready=yes)[1] == "ok"
    assert _routine_offer(store, rid, _skill(offerable=False), ready=yes)[1] == "not_offerable"
    assert _routine_offer(store, rid, _skill(sends=True), ready=yes)[1] == "sends"
    assert _routine_offer(store, rid, _skill(sends_when=({"slot": "send"},)), ready=yes)[1] == "sends"
    assert _routine_offer(store, rid, _skill(wont=("type",)), ready=yes)[1] == "unknown_wont"   # fail closed
    assert _routine_offer(store, rid, _skill(preview=""), ready=yes)[1] == "no_preview"
    assert _routine_offer(store, rid, _skill(key="other"), ready=yes)[1] == "no_skill"
    assert _routine_offer(store, rid, _skill(), ready=lambda s, k: False)[1] == "not_ready"
    assert _routine_offer(store, rid, _skill(), ready=yes, available=lambda k: False)[1] == "unavailable"
    assert _routine_offer(store, rid, _skill(), ready=yes, available=lambda k: None)[1] == "unavailable"
    bad = add_routine(store, key="meet.open|chrome", params={"volume": 40})
    assert _routine_offer(store, bad, _skill(), ready=yes)[1] == "bad_params"
    ok = add_routine(store, key="meet.open|arc", params={"pick": 1})
    offer, why = _routine_offer(store, ok, _skill(), ready=yes)
    assert why == "ok" and offer["slots"] == {"pick": 1}


def test_the_real_meet_open_data_offers_with_its_own_wont_line(store):
    rid = add_routine(store)
    offer, why = offers.routine_offer(store, routine_offers.routine(store, rid), expires_at=time.time() + 600,
                                      why="You've joined it around 08:58 on 7 of the last 9 weekdays.",
                                      ready=lambda s, k: True)
    assert why == "ok"
    assert offer["wont"] == "Won't join, unmute, type or send anything. You click Join."
    assert offer["steps"] == ["Open the Meet tab in Zen"] and offer["label"] == "Open it"
    from yuki.skills.connection import load

    skills = load()
    sending = [s for s in skills.skills if s.sends or s.sends_when]
    for s in sending:   # message: never offerable as a routine card, whatever its data says
        assert offers.routine_offer(store, SimpleNamespace(id=rid, skill_id=s.key, params={}, offer_mode="early"),
                                    expires_at=time.time() + 60, why="", ready=lambda st, k: True)[1] in (
            "sends", "not_offerable")


# -- the click: running, nothing runs in memory ---------------------------------------------------------------------


def test_act_answers_running_and_runs_nothing(store, monkeypatch):
    import yuki.skills.engine as engine

    def boom(*a, **k):
        raise AssertionError("memory ran a skill")

    monkeypatch.setattr(engine, "resolve_and_run", boom)
    monkeypatch.setattr(engine, "run", boom)
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    nid = routine_card(store, rid)
    got = MemoryClient(store).offer_act(nid)
    assert got == {"ok": True, "do": "run_skill", "state": "running", "skill": "meet.open", "slots": {},
                   "routine": rid, "undo": True}
    assert store.nudge(nid).outcome == "running"
    again = offers.act(store, nid)
    assert again["why"] == "already" and again["state"] == "running"


def test_act_checks_its_own_expiry_the_routine_and_the_trust_again(store, monkeypatch):
    trusted = {"v": True}
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: trusted["v"])
    rid = add_routine(store)
    old = routine_card(store, rid, expires_in=60.0, at=time.time() - 120)
    assert offers.act(store, old)["why"] == "expired" and store.nudge(old).outcome == "expired"
    nid = routine_card(store, rid)
    trusted["v"] = False
    assert offers.act(store, nid)["why"] == "not_ready" and store.nudge(nid).outcome is None
    trusted["v"] = True
    routine_offers.set_mode(store, rid, "quiet", why="test")
    assert offers.act(store, nid)["why"] == "gone"
    routine_offers.set_mode(store, rid, "early", why="test")
    assert offers.act(store, nid, enabled=False)["why"] == "off"
    assert offers.act(store, nid)["state"] == "running"


def test_offer_result_moves_only_running_and_keeps_a_content_free_ledger(store, monkeypatch):
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    nid = routine_card(store, rid)
    client = MemoryClient(store)
    assert client.offer_result(nid, True, {"outcome": "done"}) is False      # not pressed: nothing moves
    assert client.offer_act(nid)["state"] == "running"
    detail = {"outcome": "done", "route": "a:census:driver", "ms": 840.5, "verified": True, "focus": True,
              "title": "Daily Blocker Sync", "say": "Your Meet tab is up in Zen."}
    assert client.offer_result(nid, True, detail) is True
    assert store.nudge(nid).outcome == "done"
    assert client.offer_result(nid, False, {}) is False                       # once
    ev = routine_offers.events(store, routine_id=rid, kinds=("run_ok",))[0]
    assert ev["nudge_id"] == nid and ev["detail"] == {"outcome": "done", "route": "a:census:driver", "ms": 840.5,
                                                      "verified": True, "focus": True}
    other = routine_card(store, rid)
    client.offer_act(other)
    assert client.offer_result(other, False, {"outcome": "held_game"})
    assert store.nudge(other).outcome == "failed"
    assert routine_offers.events(store, routine_id=rid, kinds=("run_failed",))[0]["detail"] == {
        "outcome": "held_game"}


def test_undo_brings_back_the_window_and_is_recorded(store, monkeypatch):
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    nid = routine_card(store, rid)
    client = MemoryClient(store)
    client.offer_act(nid)
    assert client.offer_undo(nid)["why"] == "not_done"
    client.offer_result(nid, True, {"outcome": "done"})
    assert client.offer_undo(nid) == {"ok": True, "do": "run_skill", "state": "undone"}
    assert store.nudge(nid).outcome == "undone"
    assert offers.signal(store.nudge(nid), time.time()) == ("undone", -1.0)


def test_the_card_row_carries_its_why_line_wont_line_and_own_expiry(store):
    rid = add_routine(store)
    nid = routine_card(store, rid, expires_in=300.0)
    row = MemoryClient(store).pending_nudges()[0]
    assert row["id"] == nid and row["trigger"] == "routine"
    o = row["offer"]
    assert o["do"] == "run_skill" and o["why"] == "You've joined it around 08:58." and o["undo"] is True
    assert o["wont"] == "Won't join, unmute, type or send anything. You click Join."
    from datetime import datetime

    assert datetime.fromisoformat(o["expires_at"]).timestamp() == pytest.approx(store.nudge(nid).at + 300, abs=1)
    assert offers.resolve_ref(store, f"R{rid}")["kind"] == "routine"
    routine_offers.set_mode(store, rid, "off", why="test")
    assert offers.resolve_ref(store, f"R{rid}") is None


def test_a_hand_written_run_skill_card_without_its_routine_is_no_offer(store):
    _cid, nid = store.record_checkin(
        NudgeCheckin(at=time.time(), trigger="routine", outcome="notice", model=None),
        NewNudge(kind="notice", text="x", trigger="routine",
                 inputs={"offer": {"do": "run_skill", "label": "Open it", "steps": ["open it"], "refs": ["W3"],
                                   "skill": "meet.open", "expires_at": time.time() + 60}}))
    assert offers.stored_offer(store.nudge(nid)) is None
    assert offers.act(store, nid)["why"] == "no_offer"


def test_a_run_skill_offer_on_any_card_but_the_routine_card_is_refused(store, monkeypatch):
    """Review round 2 L4 (defence in depth): a hand-written card of another channel carrying a well-formed run_skill
    offer for a live routine is never granted ``running``."""
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    now = time.time()
    offer, why = offers.routine_offer(store, routine_offers.routine(store, rid), now, expires_at=now + 600, why="x",
                                      ready=lambda s, k: True)
    assert offer is not None, why
    for kind, trigger in (("notice", "thread"), ("notice", "plans"), ("resume", None), ("question", "checkin")):
        _cid, nid = store.record_checkin(NudgeCheckin(at=now, trigger=trigger or kind, outcome=kind, model="m"),
                                         NewNudge(kind=kind, text="x", trigger=trigger,
                                                  inputs={"offer": dict(offer), "routine_id": rid}))
        got = offers.act(store, nid, now=now + 5)
        assert got["ok"] is False and got["why"] == "no_offer", (kind, trigger)
        assert store.nudge(nid).outcome is None
    assert offers.act(store, routine_card(store, rid, at=now), now=now + 5)["state"] == "running"


def test_undo_after_a_run_the_pc_couldnt_confirm_keeps_it_failed(store, monkeypatch):
    """Review round 2 L9: a failed run may have brought a window forward: Undo is allowed (the UI brings the window
    back) and the outcome stays ``failed``, so nothing is learned against the routine or the offer kind."""
    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    nid = routine_card(store, rid)
    client = MemoryClient(store)
    client.offer_act(nid)
    assert client.offer_result(nid, False, {"outcome": "asked", "focus": True})
    assert client.offer_undo(nid) == {"ok": True, "do": "run_skill", "state": "undone"}
    n = store.nudge(nid)
    assert n.outcome == "failed" and offers.signal(n, time.time()) is None


# -- learning: the routine channel is the lane's, never the models' --------------------------------------------------


def test_routine_cards_stay_out_of_the_models_feedback_and_the_observation_budgets(store, monkeypatch):
    from yuki.memory.presence import observation_cards

    monkeypatch.setattr("yuki.skills.registry.offer_ready", lambda *a, **k: True)
    rid = add_routine(store)
    now = time.time()
    for k in range(5):
        nid = routine_card(store, rid, at=now - 3600 * (k + 1))
        store.react_to_nudge(nid, "dismissed", at=now - 3600 * (k + 1) + 10)
    assert offers.offer_scores(store, now)[("routine", "run_skill")] == -2.5
    assert offers.feedback_lines(store, lambda v, n: str(v)[:n], now) == []
    cards = store.nudges(kinds=("notice",))
    assert len(cards) == 5 and observation_cards(cards) == []


def test_routine_feedback_api_steps_the_mode_once(store):
    rid = add_routine(store)
    nid = routine_card(store, rid)
    client = MemoryClient(store)
    assert client.routine_feedback(nid, "less") == {"ok": True, "routine": rid, "mode_from": "early",
                                                    "mode_to": "late"}
    assert client.routine_feedback(nid, "less")["why"] == "already"
    assert client.routine_feedback(nid, "bogus")["why"] == "bad_kind"
    other = routine_card(store, rid)
    assert client.routine_feedback(other, "less")["mode_to"] == "quiet"
    third = routine_card(store, rid)
    assert client.routine_feedback(third, "less")["mode_to"] == "off"      # Less of these on a quiet routine
    _cid, plain = store.record_checkin(NudgeCheckin(at=time.time(), trigger="thread", outcome="notice", model="m"),
                                       NewNudge(kind="notice", text="x", trigger="thread"))
    assert client.routine_feedback(plain, "less")["why"] == "not_routine"


# -- the policy text: the work ban stays word for word ---------------------------------------------------------------


#: OFFER_RULES' work-ban sentence as it stood at main d4cf6c5 (offers.py:156-162): must not change (B.3).
WORK_BAN = (
    "The user's own work - their code, repos, files, documents, designs, reviews, tests, their apps' sessions - is "
    "theirs: never offer to do, open, read, check, run or finish any of it, and never to send, post or follow up a "
    "message (\"that's my work, I'll do it\"). Putting one of its open items on their list (todo_add), showing what "
    "you already hold about it (show_memory) or drafting a reply someone is waiting on (draft) does not touch the "
    "work and is fine.")


def test_the_work_ban_stays_word_for_word_and_the_routine_sentence_is_appended():
    assert WORK_BAN in offers.OFFER_RULES
    assert ("Opening the user's usual non-work things at the time they usually do them (their meeting tab, a "
            "playlist) is done by Yuki's routine cards, which code writes from what they measurably do; never offer "
            "it here, and never anything outside the list above.") in offers.OFFER_RULES
    assert offers.OFFER_RULES.index(WORK_BAN) < offers.OFFER_RULES.index("Opening the user's usual")
    assert "run_skill" not in offers.OFFER_RULES


def test_the_presence_and_welcome_back_prompts_allow_only_routine_cards():
    from yuki.memory.nudges import RESUME_SYSTEM_PROMPT
    from yuki.memory.presence import OBSERVE_PROMPT

    assert "never doing their work: \"that is my work, I'll do it\"" in OBSERVE_PROMPT
    assert ("Separate routine cards, which code writes, may open their usual meeting tab or music at their usual "
            "time; never offer those here.") in OBSERVE_PROMPT
    assert "never doing anything on your own" not in OBSERVE_PROMPT
    assert ("Picking their work back up is theirs: never offer to reopen or check their code, files, runs, sessions "
            "or reviews for them.") in RESUME_SYSTEM_PROMPT
    assert ("Their usual meeting or music at the usual time comes on its own card; never mention or offer it "
            "here.") in RESUME_SYSTEM_PROMPT


def test_the_todo_headings_mirror_each_other():
    from yuki.agent.memory import STANDING_SECTION_HEADINGS
    from yuki.memory.api import STANDING_HEADINGS

    agent_side = [h for h in STANDING_SECTION_HEADINGS if h.startswith("The user's own to-dos")]
    memory_side = STANDING_HEADINGS["todo"]
    assert agent_side == [memory_side] and "never offer to do one" in memory_side


def test_routines_stay_out_of_the_mcp():
    text = (ROOT / "yuki" / "mcp" / "server.py").read_text(encoding="utf-8")
    for needle in ("routine_events", "routine_offers", "FROM routines", "JOIN routines", "routines(", "routines WHERE"):
        assert needle not in text


# -- the shared gates: the routine channel ---------------------------------------------------------------------------


def test_the_routine_channel_skips_the_card_gap_never_a_card_showing_and_moves_the_gap_for_others():
    from yuki.core.attention import DEFAULT_RULES, Attention

    rule = DEFAULT_RULES["routine"]
    assert (rule.budget, rule.window_s, rule.card, rule.quiet, rule.fullscreen, rule.meeting, rule.card_gap) == (
        2, 86400.0, True, True, True, False, False)
    att = Attention(quiet_start="00:30", quiet_end="00:30", card_gap_s=600.0)
    t = 1_791_000_000.0
    att.record("coach", now=t)
    assert att.check("question", now=t + 60).reason == "card_gap"
    assert att.check("routine", now=t + 60).allowed                        # the gap is skipped
    att.card_shown("nudge:7")
    assert att.check("routine", now=t + 60).reason == "card_pending"       # never over a card showing
    att.card_closed()
    att.record("routine", now=t + 120)
    assert att.check("coach", now=t + 200).reason == "card_gap"            # its own card moves the gap for others
    att.record("routine", now=t + 300)
    assert att.check("routine", now=t + 400).reason == "budget"            # two a day
    assert att.check("routine", now=t + 120 + 86401).allowed
    assert att.check("routine", now=t + 400, facts={"meeting": True}).reason == "budget"   # meeting isn't its rule
