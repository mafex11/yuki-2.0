"""The code-only ``run_skill`` offer (yuki.memory.offers; PLAN.md Phase R, D14, B.3 ``test_offers``): no model can
ever emit it (not in any writer's schema or the judge's enum, refused from any model dict, given to the routine
channel only), a hand-written card without its routine is no offer; the policy text keeps the work ban word for word.
Temp databases only; nothing touches the desktop or a model.

r30: the routine-card tests were cut: they ran on the hand-written ``meet.open`` (gone since lSplit, 10 Oct), on a
stand-in skill that fails ``routine_gate`` (``not_learned``: routines offer only learned skills since lWire), or on the
resume and observe writers (deleted by kProactive, 18 Oct).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from _rlane_world import open_store
from yuki.memory import offers
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


# -- the click: running, nothing runs in memory ---------------------------------------------------------------------


def test_a_hand_written_run_skill_card_without_its_routine_is_no_offer(store):
    _cid, nid = store.record_checkin(
        NudgeCheckin(at=time.time(), trigger="routine", outcome="notice", model=None),
        NewNudge(kind="notice", text="x", trigger="routine",
                 inputs={"offer": {"do": "run_skill", "label": "Open it", "steps": ["open it"], "refs": ["W3"],
                                   "skill": "meet.open", "expires_at": time.time() + 60}}))
    assert offers.stored_offer(store.nudge(nid)) is None
    assert offers.act(store, nid)["why"] == "no_offer"


# -- the policy text: the work ban stays word for word ---------------------------------------------------------------


#: OFFER_RULES' work-ban sentence: must not change (B.3). r30: as jSwitch (10 Oct) reworded it when the ``draft`` offer
#: was retired (no ready-written reply, no draft of their work).
WORK_BAN = (
    "The user's own work - their code, repos, files, documents, designs, reviews, tests, their apps' sessions - is "
    "theirs: never offer to do, open, read, check, run, draft or finish any of it, never a ready-written reply for "
    "them to copy, and never to send, post or follow up a message (\"that's my work, I'll do it\"). Putting one of "
    "its open items on their list (todo_add) or showing what you already hold about it (show_memory) does not touch "
    "the work and is fine.")


def test_the_work_ban_stays_word_for_word_and_the_routine_sentence_is_appended():
    assert WORK_BAN in offers.OFFER_RULES
    assert ("Opening the user's usual non-work things at the time they usually do them (their meeting tab, a "
            "playlist) is done by Yuki's routine cards, which code writes from what they measurably do; never offer "
            "it here, and never anything outside the list above.") in offers.OFFER_RULES
    assert offers.OFFER_RULES.index(WORK_BAN) < offers.OFFER_RULES.index("Opening the user's usual")
    assert "run_skill" not in offers.OFFER_RULES


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
