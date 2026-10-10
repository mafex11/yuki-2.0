"""sVoice: preferences from the user's words or a correction, each confirmed by one "Keep this as your usual?"
(PLAN.md Phase S rules; map-skills S4.2, S4.3; redteam D14; B.2 T8).

Temp databases and sCore's invented "Demo Player" connection only (tests/skills_fixtures/demo.toml); no app, no window,
no key, no sound, no model call.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import prefs as P
from yuki.skills import registry as R

FIXTURES = Path(__file__).with_name("skills_fixtures")
CONTROL = "demo.volume:control"


@pytest.fixture()
def demo() -> C.Loaded:
    C.clear_cache()
    return C.load([FIXTURES])


@pytest.fixture()
def store(tmp_path: Path, demo: C.Loaded):
    s = Store.open(tmp_path / "memory.db")
    R.seed(s, demo)
    yield s
    s.close()


def _events(store: Store, slot: str | None = None) -> list[tuple[str, str, str, float]]:
    sql = ("SELECT p.slot, p.option, e.type, e.weight FROM pref_events e JOIN prefs p ON p.id = e.pref_id "
           + ("WHERE p.slot = ? " if slot else "") + "ORDER BY e.id")
    return [tuple(r) for r in store._query(sql, (slot,) if slot else ())]


def _run(store: Store, demo: C.Loaded, filled: dict[str, str]) -> str:
    return R.start_run(store, skill=demo.get("demo.volume"), origin="voice", filled=filled)


# -- a correction names another option: corrected (+3) for it, rejected for the one used --------------------------------


def test_a_correction_naming_another_option_writes_corrected_and_rejected(store: Store, demo: C.Loaded) -> None:
    skill = demo.get("demo.volume")
    key = _run(store, demo, {"level": "words", "control": "words"})
    got = P.note_correction(store, skill, values={"level": 40, "control": "in_app"},
                            previous={"level": 40, "control": "mixer"}, run_key=key, quote="no, the slider inside it")
    assert got == [{"pref": CONTROL, "option": "in_app", "rejected": "mixer"}]
    assert _events(store, CONTROL) == [(CONTROL, "in_app", "corrected", 3.0), (CONTROL, "mixer", "rejected", -1.0)]
    choice = P.choose(store, CONTROL)
    assert choice is not None and choice.option == "in_app" and choice.named   # most recent, named aloud until kept


def test_a_correction_of_the_amount_or_a_guessed_value_is_no_preference(store: Store, demo: C.Loaded) -> None:
    skill = demo.get("demo.volume")
    # the control came from their usual (pref), not their words: correcting the level writes nothing
    key = _run(store, demo, {"level": "words", "control": "pref"})
    assert P.note_correction(store, skill, values={"level": 30, "control": "mixer"},
                             previous={"level": 60, "control": "in_app"}, run_key=key) == []
    # the same option named again: nothing either
    key = _run(store, demo, {"level": "words", "control": "words"})
    assert P.note_correction(store, skill, values={"level": 30, "control": "mixer"},
                             previous={"level": 60, "control": "mixer"}, run_key=key) == []
    assert _events(store) == []


def test_the_slot_names_the_talker_gave_stand_in_when_the_run_has_no_ledger_row(store: Store, demo: C.Loaded) -> None:
    skill = demo.get("demo.volume")
    got = P.note_correction(store, skill, values={"control": "mixer"}, previous=None, run_key=None, given=["control"])
    assert got == [{"pref": CONTROL, "option": "mixer", "rejected": None}]
    assert P.note_correction(store, skill, values={"control": "mixer"}, previous=None, run_key=None,
                             given=["level"]) == []


def test_t8_a_cancelled_vague_request_writes_no_preference_negative(store: Store, demo: C.Loaded) -> None:
    """B.2 T8 / redteam D14: the 9 Oct 16:53 shape (a vague request, cut off; the user took the mouse and it was
    cancelled for slowness and a bad click, no alternative named). Only a correction naming another option writes a
    negative; a cancel, a failure or a plain repeat never does."""
    key = _run(store, demo, {"level": "words", "control": "pref"})
    R.finish_run(store, key, outcome="cancelled", checked=None)
    out = P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "in_app"}, previous=None,
                            run_key=key, given=["level"], corrects=False, quote="this is a vague request but",
                            loaded=demo)
    assert out == {"corrected": [], "still_card": None}
    out = P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "in_app"},
                            previous={"level": 40, "control": "in_app"}, run_key=key, given=["level"], corrects=True,
                            loaded=demo)
    assert out["corrected"] == []   # "corrects" with no other option named: still nothing
    negatives = [e for e in _events(store) if e[2] in ("rejected", "declined") or e[3] < 0]
    assert negatives == []


# -- "Keep this as your usual?" -----------------------------------------------------------------------------------------


def test_keep_usual_yes_confirms_and_just_this_time_keeps_one_choice(store: Store) -> None:
    P.record(store, CONTROL, "in_app", "said", quote="use the slider inside it")
    assert P.keep_usual(store, CONTROL, "in_app", "yes") == {"ok": True, "state": "usual"}
    assert P.usual(store, CONTROL) == "in_app"
    choice = P.choose(store, CONTROL)
    assert choice.source == "usual" and not choice.named
    P.record(store, CONTROL, "mixer", "said")
    assert P.keep_usual(store, CONTROL, "mixer", "just_this_time")["state"] == "seen"
    assert P.usual(store, CONTROL) == "in_app"   # the usual stands; "just this time" is one observation
    assert P.keep_usual(store, CONTROL, "mixer", "maybe")["ok"] is False


def test_keep_line_appends_the_one_question() -> None:
    assert P.keep_line("Demo Player at 40%.") == "Demo Player at 40%. Keep this as your usual?"
    assert P.keep_line("Demo Player at 40%") == "Demo Player at 40%. Keep this as your usual?"
    assert P.keep_line("") == P.KEEP_QUESTION


def test_a_usual_outvoted_three_times_gets_one_still_your_usual_card(store: Store, demo: C.Loaded) -> None:
    P.record(store, CONTROL, "in_app", "confirmed", at=time.time() - 100)
    for i in range(3):
        P.record(store, CONTROL, "mixer", "observed", at=time.time() - 50 + i)
    assert P.choose(store, CONTROL).still_usual
    key = _run(store, demo, {"level": "words", "control": "pref"})
    out = P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "mixer"}, previous=None,
                            run_key=key, given=["level"], loaded=demo)
    qid = out["still_card"]
    assert isinstance(qid, int)
    q = store.question(qid)
    assert q.origin == P.QUESTION_ORIGIN and "still" in q.question
    assert P.question_target(store, qid) == (CONTROL, "in_app")
    again = P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "mixer"}, previous=None,
                              run_key=key, given=["level"], loaded=demo)
    assert again["still_card"] is None   # one card per confirmation, never a second
    assert P.choose(store, CONTROL).option == "mixer"   # the asked event never weighs


# -- the extractor's closed list and its question card ----------------------------------------------------------------


def test_usual_choices_are_the_enum_slots_with_a_preference(demo: C.Loaded) -> None:
    got = P.usual_choices(demo)
    assert [c.text for c in got] == [f"{CONTROL}=in_app", f"{CONTROL}=mixer"]
    assert all(c.skill == "demo.volume" and c.pref == CONTROL for c in got)
    assert P.parse_choice(" demo.volume:control = mixer ", got).option == "mixer"
    assert P.parse_choice("demo.volume:control=loud", got) is None
    assert P.parse_choice("use the mixer", got) is None   # never a reading of words


def test_a_proposal_is_asked_once_and_confirmed_only_by_its_own_option(store: Store, demo: C.Loaded) -> None:
    choices = P.usual_choices(demo, store=store)
    mixer = next(c for c in choices if c.option == "mixer")
    in_app = next(c for c in choices if c.option == "in_app")
    assert P.propose(store, mixer, quote="always use the mixer for it", source="turn:7") is not None
    assert P.usual(store, CONTROL) is None   # proposed, never applied silently
    qid = P.ask_card(store, P.usual_question(mixer, quote="always use the mixer for it", at=time.time()), mixer)
    assert isinstance(qid, int) and store.question(qid).origin == "usual"
    assert P.pending_question(store, CONTROL, "mixer") == qid
    assert P.ask_card(store, P.usual_question(mixer, quote="again", at=time.time()), mixer) is None   # one card
    assert P.confirm_question(store, qid, in_app, quote="the slider") is False   # another option: no confirmation
    assert P.usual(store, CONTROL) is None
    assert P.confirm_question(store, qid, mixer, quote="yes keep it") is True
    assert P.usual(store, CONTROL) == "mixer"


SCOPED = """
[connection]
app = "demoaudio"

[skill."demo.loud"]
end = "volume_change"
kind = "change"
menu = "change a Demo app's volume"
done_line = "Done."
handoff = false

[[skill."demo.loud".slot]]
name = "target"
type = "enum"
options = ["demoplayer", "demochat"]

[[skill."demo.loud".slot]]
name = "control"
type = "enum"
options = ["in_app", "mixer"]
pref = "demo.loud:control@{target}"

[[skill."demo.loud".route]]
rung = "A"
via = "app_volume"
args = { app = "{target}", level = "40" }
game = "always"
done = { probe = "read_back" }
"""


def test_scoped_preference_keys_take_each_option_of_their_scope_slot(tmp_path: Path) -> None:
    (tmp_path / "scoped.toml").write_text(SCOPED, encoding="utf-8")
    C.clear_cache()
    loaded = C.load([tmp_path])
    assert loaded.get("demo.loud") is not None, loaded.errors
    texts = [c.text for c in P.usual_choices(loaded)]
    assert texts == ["demo.loud:control@demoplayer=in_app", "demo.loud:control@demoplayer=mixer",
                     "demo.loud:control@demochat=in_app", "demo.loud:control@demochat=mixer"]


def test_an_answered_card_never_swallows_the_still_your_usual_card(store: Store, demo: C.Loaded) -> None:
    """Review r1 M1 (the reviewer's still_card.py): the proposal card was answered and the usual confirmed; three later
    choices went the other way: the "still your usual?" card goes out once (a new question row, one asked tie), and
    later voice runs while it stays outvoted add nothing. A card already answered for the same subject is "known":
    no id back, no asked row."""
    mixer = next(c for c in P.usual_choices(demo, store=store) if c.option == "mixer")
    q1 = P.ask_card(store, P.usual_question(mixer, quote="use the mixer", at=1000.0), mixer, at=1000.0)
    assert isinstance(q1, int)
    store._write(lambda c: c.execute("UPDATE questions SET status = 'answered' WHERE id = ?", (q1,)))
    assert P.confirm_question(store, q1, mixer, quote="yes", at=time.time() - 100)
    # the very same card again (same subject): known, nothing written
    assert P.ask_card(store, P.usual_question(mixer, quote="use the mixer", at=1000.0), mixer, at=1000.0) is None
    for i in range(3):
        P.record(store, CONTROL, "in_app", "observed", at=time.time() - 50 + i)
    assert P.choose(store, CONTROL).still_usual
    key = _run(store, demo, {"level": "words", "control": "pref"})
    ids = [P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "mixer"}, previous=None,
                             run_key=key, given=["level"], loaded=demo)["still_card"] for _ in range(3)]
    assert isinstance(ids[0], int) and ids[0] != q1 and ids[1:] == [None, None]
    assert "still" in store.question(ids[0]).question
    assert store._query("SELECT count(*) FROM questions")[0][0] == 2
    assert store._query("SELECT count(*) FROM pref_events WHERE type = 'asked'")[0][0] == 2   # one tie per card
    # answered too: the same confirmation never asks again ...
    store._write(lambda c: c.execute("UPDATE questions SET status = 'answered' WHERE id = ?", (ids[0],)))
    assert P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "mixer"}, previous=None,
                             run_key=key, given=["level"], loaded=demo)["still_card"] is None
    assert store._query("SELECT count(*) FROM pref_events WHERE type = 'asked'")[0][0] == 2
    # ... and a new confirmation is a new card when it is outvoted again
    P.record(store, CONTROL, "mixer", "confirmed", at=time.time() - 40)
    for i in range(3):
        P.record(store, CONTROL, "in_app", "observed", at=time.time() - 30 + i)
    again = P.after_voice_run(store, "demo.volume", values={"level": 40, "control": "mixer"}, previous=None,
                              run_key=key, given=["level"], loaded=demo)["still_card"]
    assert isinstance(again, int) and again not in (q1, ids[0])
