"""sCore: the resolver (PLAN.md Phase S rules; map-skills S3.2) and the requests suite's resolver contract.

Invented data only; temp databases only (the eval resolver is built on a temp file, never the live database).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import prefs as P
from yuki.skills import registry as R
from yuki.skills.resolve import Ask, Handoff, Held, Live, Plan, Refused, done_line, resolve

FIXTURES = Path(__file__).with_name("skills_fixtures")
#: The demo list slot's taste kind, derived from its ``kind`` and the connection's app (``<app>_<kind>``, lSplit).
LIST_TASTE = "demoplayer_list"


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


def _taste(store: Store, kind: str, name: str, ref: str, hour_weight: float = 5.0) -> None:
    now = time.time()
    hist = json.dumps([hour_weight] * 24)
    store._write(lambda c: c.execute(
        "INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, hist_weekend, "
        "n_obs, updated_at) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?)",
        (kind, store.cipher.digest("taste", name), store._enc(name), store._enc(ref), hist, hist, 5.0, now - 10)))


def test_an_unknown_skill_is_refused(store: Store, demo: C.Loaded) -> None:
    assert isinstance(resolve("demo.nothing", {}, ["hi"], store=store, loaded=demo), Refused)


def test_given_values_are_checked_by_type_and_by_the_users_words(store: Store, demo: C.Loaded) -> None:
    got = resolve("demo.volume", {"level": 40, "control": "MIXER"}, ["Demo Player to 40 on the mixer"],
                  store=store, loaded=demo)
    assert isinstance(got, Plan)
    assert got.values == {"level": 40.0, "control": "mixer"} and got.filled == {"level": "words", "control": "words"}
    assert [r.key for r in got.routes] == ["A:mixer:app_volume"]          # the in_app rung's when-condition
    bad = resolve("demo.volume", {"level": 400, "control": "laser"}, ["set it"], store=store, loaded=demo)
    assert isinstance(bad, Ask) and bad.question == "What level?"         # dropped, then required: asked


def test_an_entity_must_be_said_or_listed_never_invented(store: Store, demo: C.Loaded) -> None:
    _taste(store, LIST_TASTE, "Invented Rainy Mix", "demoplayer://list/r1")
    said = resolve("demo.play", {"list": "Sunday Tapes"}, ["play my Sunday Tapes list"], store=store, loaded=demo)
    assert isinstance(said, Plan) and said.values["list"] == {"name": "Sunday Tapes"} and said.filled["list"] == "words"
    listed = resolve("demo.play", {"list": "invented rainy mix"}, ["play something calm"], store=store, loaded=demo)
    assert listed.values["list"]["ref"] == "demoplayer://list/r1"        # a menu name: its learned link too
    assert [r.key for r in listed.routes] == ["A:library:uia_invoke"]
    assert listed.skipped == {"B:link:open_uri": "untrusted"}             # rung B waits for one verified run
    play = demo.get("demo.play")
    R.import_evidence(store, play.key, play.routes[1], ok=1, source="procedure:24")
    trusted = resolve("demo.play", {"list": "invented rainy mix"}, ["play something calm"], store=store, loaded=demo)
    assert [r.key for r in trusted.routes] == ["A:library:uia_invoke", "B:link:open_uri"]
    no_link = resolve("demo.play", {"list": "Sunday Tapes"}, ["play my Sunday Tapes list"], store=store, loaded=demo)
    assert no_link.skipped["B:link:open_uri"] == "when"                   # no learned link: never a guessed one
    # Phase 2.1 (18 Oct, resolve.FROM_READING): on an undoable run a name the words don't hold is the caller's own
    # reading, kept as given and tagged "reading" (a guess: the done line names it and one "not that" takes the next),
    # never swapped for a taste pick; with no learned link for it, rung B never guesses one
    invented = resolve("demo.play", {"list": "Totally Made Up"}, ["play something"], store=store, loaded=demo)
    assert isinstance(invented, Plan) and invented.dropped == []
    assert invented.values["list"] == {"name": "Totally Made Up"} and invented.filled["list"] == "reading"
    assert invented.sources["list"] == "reading" and invented.skipped["B:link:open_uri"] == "when"


def test_fill_order_told_then_preference_and_a_means_slot_has_no_default(store: Store, demo: C.Loaded) -> None:
    # r30: the control is a means slot, which ships no default (lSplit, 10 Oct): with no words and no choice of theirs
    # it stays unset, and no rung of the demo file runs without it
    plain = resolve("demo.volume", {"level": 30}, ["thirty"], store=store, loaded=demo)
    assert isinstance(plain, Handoff) and "control" not in plain.values and "control" not in plain.filled
    P.record(store, "demo.volume:control", "mixer", "observed")
    recent = resolve("demo.volume", {"level": 30}, ["thirty"], store=store, loaded=demo)
    assert recent.values["control"] == "mixer" and recent.filled["control"] == "pref" and "control" in recent.named
    P.keep_usual(store, "demo.volume:control", "mixer", "yes")
    usual = resolve("demo.volume", {"level": 30}, ["thirty"], store=store, loaded=demo)
    assert usual.filled["control"] == "pref" and "control" not in usual.named
    assert done_line(usual.skill, usual.values, usual.named) == "Demo Player at 30%."
    assert done_line(recent.skill, recent.values, recent.named) == "Demo Player at 30% with its mixer control."


def test_the_choice_rule(store: Store) -> None:
    slot = "invented.skill:browser"
    assert P.choose(store, slot) is None
    for _ in range(3):
        P.record(store, slot, "invented_a", "observed")
    got = P.choose(store, slot)
    assert (got.option, got.source, got.named) == ("invented_a", "observed", False)
    P.record(store, slot, "invented_b", "observed")
    got = P.choose(store, slot)                                           # 3 of 4 < 0.8: the most recent, named
    assert (got.option, got.source, got.named) == ("invented_b", "recent", True)
    P.keep_usual(store, slot, "invented_a", "yes")
    assert P.choose(store, slot).source == "usual"
    for _ in range(3):
        P.record(store, slot, "invented_b", "observed")
    after = P.choose(store, slot)
    assert after.source != "usual" and after.still_usual                  # outvoted 3 times: the question is due
    assert P.keep_usual(store, slot, "invented_b", "just_this_time")["state"] == "seen"


def test_a_direction_with_no_amount_takes_the_users_step_else_the_default(store: Store, demo: C.Loaded) -> None:
    sk = C.Skill(**{**demo.get("demo.volume").__dict__,
                    "slots": demo.get("demo.volume").slots + (C.Slot(name="change", type="points"),)})
    loaded = C.Loaded((sk,), (), "x")
    got = resolve("demo.volume", {"level": 50, "change": "down"}, ["quieter"], store=store, loaded=loaded,
                  default_step=10)
    assert got.values["change"] == -10.0
    P.keep_usual(store, "demo.volume:change@step", "4", "yes")
    assert resolve("demo.volume", {"level": 50, "change": "up"}, ["louder"], store=store, loaded=loaded
                   ).values["change"] == 4.0


def test_live_playing_fills_one_and_asks_between_two(demo: C.Loaded, store: Store) -> None:
    sk = C.Skill(**{**demo.get("demo.volume").__dict__,
                    "slots": demo.get("demo.volume").slots + (C.Slot(name="target", type="app", live="playing",
                                                                         ask_in_call=True, ask="Which app?"),)})
    loaded = C.Loaded((sk,), (), "x")
    one = resolve("demo.volume", {"level": 20}, ["quieter"], store=store, loaded=loaded,
                  live=Live(playing=("demoplayer",)))
    assert one.values["target"] == "demoplayer" and one.filled["target"] == "live"
    two = resolve("demo.volume", {"level": 20}, ["quieter"], store=store, loaded=loaded,
                  live=Live(playing=("demoplayer", "otherplayer")))
    assert isinstance(two, Ask) and two.options == ["demoplayer", "otherplayer"]
    call = resolve("demo.volume", {"level": 20}, ["quieter"], store=store, loaded=loaded, live=Live(in_call=True))
    assert isinstance(call, Ask) and call.question == "Which app?"
    named = resolve("demo.volume", {"level": 20, "target": "Demo Player", "control": "in_app"},
                    ["Demo Player quieter"], store=store, loaded=loaded, live=Live(in_call=True))
    assert isinstance(named, Plan) and named.values["target"] == "demo player"


def test_with_a_game_in_front_only_no_focus_rungs_the_data_allows_never_the_thinker(store: Store,
                                                                                   demo: C.Loaded) -> None:
    covered = Live(game="invented-game", window_state=lambda app: "covered")
    in_app = {"level": 40, "control": "in_app"}                           # r30: the means slot has no default
    got = resolve("demo.volume", in_app, ["forty"], store=store, loaded=demo, live=covered)
    assert isinstance(got, Plan) and [r.key for r in got.routes] == ["A:in_app:uia_invoke"]
    assert got.handoff is False                                           # D17(2)
    minimised = Live(game="invented-game", window_state=lambda app: "minimised")
    held = resolve("demo.volume", in_app, ["forty"], store=store, loaded=demo, live=minimised)
    assert isinstance(held, Held) and held.say.endswith("after the round?")
    mixer = resolve("demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"], store=store,
                    loaded=demo, live=minimised)
    assert [r.key for r in mixer.routes] == ["A:mixer:app_volume"]       # "always": the mixer runs mid-game
    home = resolve("demo.home", {}, ["open demo home"], store=store, loaded=demo, live=covered)
    assert isinstance(home, Held)                                         # a URI takes focus


def test_no_usable_rung_hands_over_unless_the_skill_says_never(store: Store, demo: C.Loaded) -> None:
    store._write(lambda c: c.execute("UPDATE skill_routes SET disabled_at = ?, disabled_why = 'x'", (time.time(),)))
    got = resolve("demo.volume", {"level": 40, "control": "mixer"}, ["forty"], store=store, loaded=demo)
    assert isinstance(got, Handoff) and got.why == "no_route"
    assert isinstance(resolve("demo.home", {}, ["home"], store=store, loaded=demo), Refused)


def test_again_repeats_the_last_runs_numbers_within_ten_minutes(store: Store, demo: C.Loaded) -> None:
    skill = demo.get("demo.volume")
    key = R.start_run(store, skill=skill, origin="voice", t0=time.time() - 60)
    R.finish_run(store, key, outcome="done", checked=True, route=skill.routes[1], evidence={"numbers": {"level": 35.0}})
    got = resolve("demo.volume", {}, ["again"], store=store, loaded=demo, again=True)
    assert got.values["level"] == 35.0 and got.filled["level"] == "again"


def test_words_naming_another_option_than_the_usual_ask_to_keep_it(store: Store, demo: C.Loaded) -> None:
    P.keep_usual(store, "demo.volume:control", "in_app", "yes")
    same = resolve("demo.volume", {"level": 40, "control": "in_app"}, ["forty in app"], store=store, loaded=demo)
    assert same.ask_keep is None
    other = resolve("demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"], store=store, loaded=demo)
    assert other.ask_keep == {"slot": "control", "pref": "demo.volume:control", "option": "mixer"}


def test_the_resolver_is_fast(store: Store, demo: C.Loaded) -> None:
    _taste(store, LIST_TASTE, "Invented Rainy Mix", "demoplayer://list/r1")
    times = []
    for i in range(40):
        t0 = time.perf_counter()
        resolve("demo.play" if i % 2 else "demo.volume", {"level": 10 + i, "list": "Invented Rainy Mix"},
                ["play the Invented Rainy Mix at", str(10 + i)], store=store, loaded=demo)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    assert times[int(len(times) * 0.95) - 1] < 50.0                      # PLAN B.2: p95 < 50 ms


# -- the requests suite's resolver ------------------------------------------------------------------------------------


class _Client:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.calls.append(params)
        block = SimpleNamespace(type="tool_use", name=self.answer["name"], input=self.answer.get("input", {}), id="t")
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=3000,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(content=[block], usage=usage)


def test_eval_resolver_contract_on_a_copy(tmp_path: Path, demo: C.Loaded) -> None:
    from yuki.skills.resolve import eval_resolver

    with pytest.raises(ValueError):
        eval_resolver(None)
    client = _Client({"name": "run_skill", "input": {"skill": "demo.volume", "slots": {"level": 25}}})
    r = eval_resolver(db_path=tmp_path / "copy" / "memory.db", client=client, model="invented-model",
                      prompt=lambda menu: "PROMPT\n" + menu, connections=[FIXTURES])
    got = r.resolve("Demo Player to 25", time.time())
    # r30: the control is a means slot with no default (lSplit), so a fresh copy fills only the level
    assert got["skill"] == "demo.volume" and got["slots"] == {"level": 25.0}
    assert "cost_usd" in got
    sent = client.calls[0]
    assert "- demo.volume:" in sent["system"][0]["text"] and sent["tools"][0]["name"] == "run_skill"
    assert sent["tools"][0]["input_schema"]["properties"]["skill"]["enum"] == list(demo.keys)
    none = eval_resolver(db_path=tmp_path / "copy2" / "memory.db", client=_Client({"name": "tell_yuki"}),
                         model="m", connections=[FIXTURES]).resolve("what's the weather", time.time())
    assert none["skill"] is None
    typical, worst = r.estimate_usd("Demo Player to 25")
    assert 0 < typical < worst


def test_eval_resolver_never_opens_the_live_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from yuki.eval import dbcopy
    from yuki.skills.resolve import eval_resolver

    monkeypatch.setattr(dbcopy, "is_live", lambda p: True)
    with pytest.raises(RuntimeError):
        eval_resolver(db_path=tmp_path / "memory.db")


def test_an_entity_usual_is_kept_by_its_taste_key_never_its_name(store: Store, demo: C.Loaded) -> None:
    from yuki.skills.resolve import from_pref, pref_option

    sk = C.Skill(**{**demo.get("demo.play").__dict__,
                    "slots": (C.Slot(name="list", type="entity", taste=LIST_TASTE, pref="demo.play:list"),)})
    loaded = C.Loaded((sk,), (), "x")
    _taste(store, LIST_TASTE, "Invented Rainy Mix", "demoplayer://list/r1")
    said = resolve("demo.play", {"list": "Invented Rainy Mix"}, ["play Invented Rainy Mix"], store=store, loaded=loaded)
    option = said.ask_keep["option"]
    assert option == store.cipher.digest("taste", "Invented Rainy Mix") and "Rainy" not in option
    unknown = resolve("demo.play", {"list": "Sunday Tapes"}, ["play Sunday Tapes"], store=store, loaded=loaded)
    assert unknown.ask_keep is None                                       # no key: never a name in clear
    P.keep_usual(store, "demo.play:list", option, "yes")
    back = resolve("demo.play", {}, ["play my usual"], store=store, loaded=loaded)
    assert back.values["list"]["name"] == "Invented Rainy Mix" and back.filled["list"] == "pref"
    assert pref_option(C.Slot(name="p", type="person"), {"name": "x"}) is None
    assert from_pref(store, C.Slot(name="t", type="text"), "x") == (False, None)
