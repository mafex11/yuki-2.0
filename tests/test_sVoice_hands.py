"""sVoice: the talker's skill hands (PLAN.md Phase S S-1, D10, D17, D18, D19; map-skills S3.4, S4.2, S4.3, S5).

``run_skill`` runs one of the user's skills through the engine (``yuki.skills.engine.resolve_and_run``): "done" said by
code only on a seen result, "couldn't confirm" said plainly, a game in front held ("after the round?"), a hand-off to
the thinker with the skill pinned, a correction written as a preference, "Keep this as your usual?" bound to the
talker's ``keep_usual``; the talker's own mixer hand and volume keys off when the volume skill is on (back without).

Stub verbs and probes, sCore's invented "Demo Player" connection, temp databases. No app, window, key, click or sound.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import donecheck
from yuki.skills import engine as E
from yuki.skills import prefs as P
from yuki.skills import registry as R
from yuki.voice import hands as H

FIXTURES = Path(__file__).with_name("skills_fixtures")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing reaches the desktop: every verb and live probe is a stand-in unless a test sets its own; the skills come
    from the invented fixture folder; the local game read says none."""
    for name in list(E.VERB_FUNCS):
        monkeypatch.setitem(E.VERB_FUNCS, name, lambda call: E.RouteResult(ran=False, outcome="no_live_verb_in_tests"))
    for name in list(donecheck.PROBE_FUNCS):
        if name != "read_back":
            monkeypatch.setitem(donecheck.PROBE_FUNCS, name, lambda ctx, args: (None, {"status": "test_stub"}))
    for name in list(donecheck.BASELINES):
        monkeypatch.setitem(donecheck.BASELINES, name, lambda args: None)
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "game_in_front", lambda: "")
    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    yield
    C.clear_cache()


class Mem:
    """A MemoryAccess stand-in over a temp MemoryClient: ``call`` runs at once; the acting marker is recorded."""

    def __init__(self, client: MemoryClient) -> None:
        self.client = client
        self.session_id = "s-test"
        self.marks: list[tuple[str, str]] = []

    def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
        return fn(self.client)

    def acting_begin(self, token: str, request: str, *, lane: str = "") -> bool:
        self.marks.append(("begin", token))
        return True

    def acting_end(self, token: str) -> bool:
        self.marks.append(("end", token))
        return True


class Desk:
    """A stand-in desk: no windows, a game in front when told."""

    def __init__(self) -> None:
        self.game = ""

    def game_front(self) -> str:
        return self.game

    def get_desktop_overview(self) -> dict:
        return {"windows": [], "foreground_hwnd": None}

    def format_overview(self, overview: dict) -> str:
        return ""


class Bridge:
    """The bits of the voice bridge the hands use."""

    def __init__(self) -> None:
        self.gate = None
        self.busy_tools = 0
        self.desk = None
        self.escalated: list[dict] = []

    def desk_job(self) -> str | None:
        return self.desk

    def may_ack(self) -> bool:
        return False

    def acked(self) -> None:
        pass

    async def _ack_if_silent(self, params: Any, args: dict) -> None:
        pass

    async def escalate(self, params: Any, name: str, args: dict, report: str, acked: bool = False,
                       context: str | None = None, skill: dict | None = None) -> bool:
        self.escalated.append({"name": name, "skill": skill})
        return True


class Params:
    def __init__(self, name: str, args: dict) -> None:
        self.function_name = name
        self.arguments = args
        self.tool_call_id = f"call-{name}"
        self.results: list[tuple[dict, Any]] = []
        self.pushed: list[Any] = []
        self.llm = SimpleNamespace(push_frame=self._push)

    async def _push(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))

    @property
    def said(self) -> list[str]:
        return [getattr(f, "text", "") for f in self.pushed]


class Verb:
    def __init__(self, result: E.RouteResult) -> None:
        self.result = result
        self.calls: list[E.RouteCall] = []

    def __call__(self, call: E.RouteCall) -> E.RouteResult:
        self.calls.append(call)
        return self.result


@pytest.fixture()
def rig(tmp_path: Path):
    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(client.store, C.load())
    desk, bridge, mem = Desk(), Bridge(), Mem(client)
    hands = H.Hands(desk)
    hands.bridge = bridge
    hands._dispatcher = SimpleNamespace(overview_text=lambda: ("", {"foreground_hwnd": None}, 0.0))
    hands.skill_keys = ("demo.volume", "demo.home", "demo.play")
    hands.skill_memory = mem
    hands.skill_settings = SimpleNamespace(skills=True, skills_volume_step=10, skills_disable_after=2)
    hands.words = lambda: ["set demo player to 40 with the mixer"]
    hands.turn_key = iter(f"s-test-{n}" for n in range(1, 99)).__next__
    yield SimpleNamespace(hands=hands, desk=desk, bridge=bridge, mem=mem, client=client)
    client.close()


def _run(hands: H.Hands, args: dict, name: str = "run_skill") -> Params:
    params = Params(name, args)
    asyncio.run(hands.handle(params))
    return params


def _level_probe(value: bool | None):
    return lambda ctx, args: (value, {"status": "stub"})


# -- the hands the talker gets -------------------------------------------------------------------------------------------


def test_with_the_volume_skill_the_mixer_hand_and_the_volume_keys_are_off() -> None:
    tools = H.talker_hand_tools(["volume", "spotify.playlist"])
    names = [t["name"] for t in tools]
    assert "app_volume" not in names and names.count("run_skill") == 1 and "keep_usual" in names
    media = next(t for t in tools if t["name"] == "media_key")
    assert media["properties"]["key"]["enum"] == ["playpause", "nexttrack", "prevtrack", "stop"]
    run = next(t for t in tools if t["name"] == "run_skill")
    assert run["properties"]["skill"]["enum"] == ["volume", "spotify.playlist"]
    assert "steps" not in run["properties"] and "say_when_done" not in run["properties"]   # code says the done line
    # back with skills = false (no menu): the hands as before phase S, the step-list hand only on the old path
    old = H.talker_hand_tools([])
    assert "app_volume" in [t["name"] for t in old] and "run_skill" not in [t["name"] for t in old]
    media_old = next(t for t in old if t["name"] == "media_key")
    assert "volumeup" in media_old["properties"]["key"]["enum"]
    learned = H.talker_hand_tools([], learned_ways=True)
    assert next(t for t in learned if t["name"] == "run_skill") is H.RUN_SKILL_TOOL
    # skills without volume: the mixer hand stays (every target is still covered)
    assert "app_volume" in [t["name"] for t in H.talker_hand_tools(["spotify.playlist"])]


def test_a_volume_key_or_the_mixer_hand_written_anyway_is_refused(rig: Any) -> None:
    rig.hands.skill_keys = ("volume",)
    ok, report, facts = rig.hands._run("media_key", {"key": "volumeup", "say_when_done": ""})
    assert not ok and facts.get("refused") and "run_skill volume" in report
    ok, report, facts = rig.hands._run("app_volume", {"app": "chrome.exe", "change": -10, "say_when_done": ""})
    assert not ok and facts.get("refused")


# -- done only on a seen result, said by code ----------------------------------------------------------------------------


def test_done_line_is_said_by_code_only_when_the_check_saw_it(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mixer = Verb(E.RouteResult(ran=True, outcome="set", verified=True, value=40.0))
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", mixer)
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    assert mixer.calls, "the mixer rung ran"
    assert params.said and params.said[0].startswith("Demo Player at 40%")
    result, props = params.results[-1]
    assert result["said"] == params.said[0] and props.run_llm is False
    rows = rig.client.store._query("SELECT outcome, checked, request_id FROM skill_runs")
    assert [tuple(r) for r in rows] == [("done", 1, "s-test-1")]


def test_couldnt_confirm_is_said_plainly_and_never_done(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=True, outcome="set")))   # no read-back
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(None))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    assert params.said == [E.LINE_UNVERIFIED]
    assert "Demo Player at 40%" not in " ".join(params.said)
    assert rig.client.store._query("SELECT outcome, checked FROM skill_runs")[0][0] == "unverified"


def test_done_said_implies_the_check_returned_true_over_random_outcomes(rig: Any,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    rng = random.Random(7)
    for _ in range(30):
        verdict = rng.choice([True, False, None])
        ran = rng.choice([True, False])
        monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=ran, outcome="set", verified=verdict)))
        monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(verdict))
        params = _run(rig.hands, {"say_first": "", "skill": "demo.volume",
                                  "slots": {"level": rng.choice([20, 40, 60]), "control": "mixer"}})
        said = " ".join(params.said)
        if "Demo Player at" in said:
            assert ran and verdict is True
    rows = rig.client.store._query("SELECT count(*) FROM skill_runs WHERE outcome = 'done' AND checked IS NOT 1")
    assert rows[0][0] == 0


# -- a game in front ----------------------------------------------------------------------------------------------------


def test_with_a_game_in_front_only_no_focus_skills_run_the_rest_say_after_the_round(
        rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = Verb(E.RouteResult(ran=True, outcome="opened"))
    monkeypatch.setitem(E.VERB_FUNCS, "open_uri", uri)
    rig.desk.game = "VALORANT.exe"
    params = _run(rig.hands, {"say_first": "", "skill": "demo.home", "slots": {}})
    assert params.said == ["You're in a game: after the round?"] and not uri.calls
    assert rig.mem.marks == []                       # nothing took focus, no acting marker
    assert rig.bridge.escalated == []                # never handed to the thinker with a game in front (D17(2))
    # the volume skill's mixer rung takes no focus and may run with a game in front
    mixer = Verb(E.RouteResult(ran=True, outcome="set", verified=True))
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", mixer)
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 30, "control": "mixer"}})
    assert mixer.calls and params.said[0].startswith("Demo Player at 30%")


# -- the thinker gets the skill pinned when its routes didn't get there --------------------------------------------------


def test_routes_that_fail_hand_over_with_the_skill_pinned(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", Verb(E.RouteResult(ran=False, outcome="not_found")))
    monkeypatch.setitem(E.VERB_FUNCS, "uia_steps", Verb(E.RouteResult(ran=False, outcome="not_found")))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "in_app"}})
    assert rig.bridge.escalated and rig.bridge.escalated[0]["skill"]["key"] == "demo.volume"
    result, _props = params.results[-1]
    assert result.get("passed_on") is True and not params.said


# -- D18: the acting marker only for a rung that takes focus -------------------------------------------------------------


def test_the_acting_marker_goes_up_only_before_a_focus_rung(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=True, outcome="set", verified=True)))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    assert rig.mem.marks == []                       # a sound-only run publishes no marker
    monkeypatch.setitem(E.VERB_FUNCS, "open_uri", Verb(E.RouteResult(ran=True, outcome="opened")))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "foreground", _level_probe(True))
    _run(rig.hands, {"say_first": "", "skill": "demo.home", "slots": {}})
    assert [m[0] for m in rig.mem.marks] == ["begin", "end"]


def test_a_focus_rung_waits_while_the_thinker_works_on_the_desktop(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = Verb(E.RouteResult(ran=True, outcome="opened"))
    monkeypatch.setitem(E.VERB_FUNCS, "open_uri", uri)
    rig.bridge.desk = "task 3 on the desktop"
    params = _run(rig.hands, {"say_first": "", "skill": "demo.home", "slots": {}})
    assert not uri.calls                             # two hands at once would collide
    mixer = Verb(E.RouteResult(ran=True, outcome="set", verified=True))
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", mixer)
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    assert mixer.calls and params.said[0].startswith("Demo Player at 40%")   # a no-focus rung runs beside it


# -- preferences: "Keep this as your usual?" and corrections -------------------------------------------------------------


def test_a_new_usual_from_their_words_asks_once_and_keep_usual_confirms(rig: Any,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=True, outcome="set", verified=True)))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    assert params.said[0].endswith("Keep this as your usual?")
    assert rig.hands.pending_keep and rig.hands.pending_keep["option"] == "mixer"
    assert "Keep this as your usual?" in (rig.hands.skills_now() or "")
    keep = _run(rig.hands, {"answer": "yes"}, name="keep_usual")
    assert keep.said == ["Okay, that's your usual now."]
    assert P.usual(rig.client.store, "demo.volume:control") == "mixer"
    assert rig.hands.pending_keep is None
    again = _run(rig.hands, {"answer": "yes"}, name="keep_usual")   # nothing waits now: nothing kept, the talker answers
    assert not again.said and "no_change" in again.results[-1][0]
    # the usual named again from their words: no second question
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 50, "control": "mixer"}})
    assert not params.said[0].endswith("Keep this as your usual?")


def test_a_correction_naming_another_option_updates_the_preference_not_a_lesson(
        rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=True, outcome="set", verified=True)))
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", Verb(E.RouteResult(ran=True, outcome="set", verified=True)))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    rig.hands.words = lambda: ["no, the slider inside it"]
    params = _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "in_app"},
                              "corrects": True})
    events = [tuple(r) for r in rig.client.store._query(
        "SELECT p.option, e.type FROM pref_events e JOIN prefs p ON p.id = e.pref_id WHERE e.type IN "
        "('corrected', 'rejected') ORDER BY e.id")]
    assert events == [("in_app", "corrected"), ("mixer", "rejected")]
    assert params.said[0].endswith("Keep this as your usual?")
    marked = rig.client.store._query("SELECT count(*) FROM skill_runs WHERE recheck = 0")[0][0]
    assert marked == 1                               # D19: the corrected run is a false done in the ledger
    assert rig.client.store._query("SELECT count(*) FROM playbooks")[0][0] == 0   # no text pitfall anywhere


def test_skills_now_is_short_and_names_the_last_run(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert rig.hands.skills_now() is None
    monkeypatch.setitem(E.VERB_FUNCS, "app_volume", Verb(E.RouteResult(ran=True, outcome="set", verified=True)))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", _level_probe(True))
    _run(rig.hands, {"say_first": "", "skill": "demo.volume", "slots": {"level": 40, "control": "mixer"}})
    line = rig.hands.skills_now() or ""
    assert line.startswith("Skills now: last run_skill demo.volume") and "level 40" in line and "done" in line
    assert len(line) <= H.SKILLS_NOW_CHARS
    rig.hands.last_skill["values"] = {"level": 40, "control": "x" * 400}
    assert len(rig.hands.skills_now() or "") <= H.SKILLS_NOW_CHARS


def test_the_old_learned_way_hand_is_untouched_without_a_menu(rig: Any) -> None:
    rig.hands.skill_keys = ()
    params = _run(rig.hands, {"say_first": "", "skill": "pb:9", "steps": [], "sends": False, "say_when_done": ""})
    result, _props = params.results[-1]
    assert "not on the Learned ways line" in str(result)   # the v3.18 path answered, not the skill engine
    assert rig.client.store._query("SELECT count(*) FROM skill_runs")[0][0] == 0


def test_a_skill_that_sends_or_joins_a_call_passes_yukis_own_check_first(rig: Any,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 8: a send (the skill's data says it sends) or a voice-channel join (its end state is call_join) goes through
    the authorization check with the user's heard words; held, nothing runs and the talker asks the check's question."""
    asked: list[tuple[str, dict, dict, bool]] = []

    def authorize(tool: str, tool_input: dict, target: dict | None = None, earlier: Any = None, *,
                  force: bool = False) -> Any:
        asked.append((tool, tool_input, target or {}, force))
        return SimpleNamespace(allowed=False, question="Send it to them now?", why="not plainly asked")

    monkeypatch.setattr(rig.hands, "authorize", authorize)
    uri = Verb(E.RouteResult(ran=True, outcome="opened"))
    monkeypatch.setitem(E.VERB_FUNCS, "open_uri", uri)
    monkeypatch.setattr(C, "sends_with", lambda skill, values, partial=False: skill.key == "demo.home")
    params = _run(rig.hands, {"say_first": "", "skill": "demo.home", "slots": {}})
    assert asked and asked[0][0] == "run_skill" and asked[0][1]["skill"] == "demo.home" and asked[0][3] is False
    assert not uri.calls and not params.said
    result, _props = params.results[-1]
    assert "held" in result and "Send it to them now?" in result["how_to_handle_it"]
    # a join (end state call_join) is judged even where the policy would let it through
    real_load = C.load

    class Joined:
        def get(self, key: str) -> Any:
            got = real_load().get(key)
            return SimpleNamespace(key=key, app=got.app, end="call_join", sends=False, sends_when=()) if got else None

    monkeypatch.setattr(C, "sends_with", lambda skill, values, partial=False: False)
    monkeypatch.setattr(C, "load", lambda dirs=None: Joined())
    asked.clear()
    params = _run(rig.hands, {"say_first": "", "skill": "demo.home", "slots": {}})
    assert asked and asked[0][3] is True and not uri.calls
