"""sCore: the skill engine (PLAN.md Phase S; D17, D18, D19; B.2 offline bar: the game tests, None never disables a
rung, "done" said => the done-check returned True, the H1 slider case, content-free logs).

Stub verbs and probes only (``RunContext.verbs`` / ``.probes``): no app, no window, no key, no click, no sound. Temp
databases only; invented names only.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import engine as E
from yuki.skills import registry as R
from yuki.skills.resolve import Handoff, Live, Plan, resolve

FIXTURES = Path(__file__).with_name("skills_fixtures")
SECRET_WORDS = ("Zanzibar", "Quillfeather", "Marigold Tapes")


@pytest.fixture(autouse=True)
def _no_live_verbs_or_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in these tests reaches the desktop: every verb and live probe the engine would dispatch to by name is a
    stand-in unless a test supplies its own (a test calling a verb function directly stubs that verb's internals)."""
    from yuki.skills import donecheck
    from yuki.skills import engine as engine_mod

    for name in list(engine_mod.VERB_FUNCS):
        monkeypatch.setitem(engine_mod.VERB_FUNCS, name,
                            lambda call: engine_mod.RouteResult(ran=False, outcome="no_live_verb_in_tests"))
    for name in list(donecheck.PROBE_FUNCS):
        if name != "read_back":
            monkeypatch.setitem(donecheck.PROBE_FUNCS, name, lambda ctx, args: (None, {"status": "test_stub"}))
    for name in list(donecheck.BASELINES):
        monkeypatch.setitem(donecheck.BASELINES, name, lambda args: None)


@pytest.fixture()
def demo() -> C.Loaded:
    C.clear_cache()
    return C.load([FIXTURES])


@pytest.fixture()
def client(tmp_path: Path, demo: C.Loaded):
    c = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(c.store, demo)
    yield c
    c.close()


class Probe:
    """A probe stub answering from a script (the last answer repeats) and recording every read."""

    def __init__(self, *answers: bool | None) -> None:
        self.answers = list(answers) or [None]
        self.reads: list[bool | None] = []

    def __call__(self, ctx: Any, args: Any) -> tuple[bool | None, dict]:
        v = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        self.reads.append(v)
        return v, {"status": "stub"}


class Verb:
    def __init__(self, result: E.RouteResult | None = None, *, on_call: Any = None) -> None:
        self.result = result or E.RouteResult(ran=True, outcome="sent")
        self.calls: list[E.RouteCall] = []
        self.on_call = on_call

    def __call__(self, call: E.RouteCall) -> E.RouteResult:
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)
        return self.result


class Clock:
    """A stand-in clock: a wait's sleep moves it on (no real waiting, no busy loop)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += float(s)


def _ctx(client: Any, *, verbs: dict | None = None, probes: dict | None = None, game: Any = None,
         origin: str = "voice", log: list | None = None, words: list[str] | None = None) -> E.RunContext:
    logs = log if log is not None else []
    clock = Clock()
    return E.RunContext(origin=origin, words=words or ["invented words"], memory=client, verbs=verbs or {},
                        probes=probes or {}, game=game or (lambda: ""), recheck="inline",
                        log=lambda event, **f: logs.append((event, f)), clock=clock.now, sleep=clock.sleep)


def _plan(client: Any, demo: C.Loaded, key: str, slots: dict, words: list[str], live: Live | None = None) -> Any:
    return resolve(key, slots, words, store=client.store, loaded=demo, live=live or Live())


def _rows(client: Any, key: str = "demo.volume") -> list[dict]:
    return [dict(r) for r in client.store._query("SELECT * FROM skill_runs WHERE skill = ? ORDER BY id", (key,))]


# -- the ladder ---------------------------------------------------------------------------------------------------


def test_a_rung_that_cannot_run_passes_to_the_next_and_done_only_on_a_seen_result(client, demo) -> None:
    vol = demo.get("demo.volume")
    R.import_evidence(client.store, vol.key, vol.routes[2], ok=1, source="procedure:72")
    invoke = Verb(E.RouteResult(ran=False, outcome="missing"))
    steps = Verb(E.RouteResult(ran=True, outcome="steps_ok"))
    level = Probe(False, False, True)
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["Demo Player to 40"])
    res = E.run(plan, _ctx(client, verbs={"uia_invoke": invoke, "uia_steps": steps}, probes={"level": level}))
    assert res.outcome == "done" and res.done and res.route == "B:steps:uia_steps"
    assert res.done_line == "Demo Player at 40% with its in_app control."   # a default is named: one word fixes it
    assert len(invoke.calls) == 1 and len(steps.calls) == 1
    assert res.tried == [{"route": "A:in_app:uia_invoke", "rung": "A", "via": "uia_invoke", "outcome": "missing"}]
    rows = _rows(client)
    assert len(rows) == 1                                                  # one ledger row per run
    final = rows[0]
    assert (final["outcome"], final["checked"], final["rung"], final["recheck"]) == ("done", 1, "B", None)
    tried = [dict(r) for r in client.store._query("SELECT * FROM skill_route_tries")]
    assert [(r["rung"], r["checked"], r["run_key"]) for r in tried] == [("A", None, final["run_key"])]


def test_a_check_that_couldnt_read_after_acting_says_so_and_stops_the_ladder(client, demo) -> None:
    vol = demo.get("demo.volume")
    R.import_evidence(client.store, vol.key, vol.routes[2], ok=1, source="procedure:72")
    invoke, steps = Verb(), Verb()
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["Demo Player to 40"])
    for _ in range(6):
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": invoke, "uia_steps": steps},
                               probes={"level": Probe(None)}))
        assert res.outcome == "unverified" and not res.done and res.say == E.LINE_UNVERIFIED
    assert steps.calls == []                                               # never done twice
    t = R.trust(client.store, vol.key)[vol.routes[0].key]
    assert t.unverified == 6 and t.disabled_at is None                     # None never disables a rung


def test_contradicted_checks_count_and_disable_after_two(client, demo) -> None:
    vol = demo.get("demo.volume")
    plan = _plan(client, demo, "demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"])
    mixer = Verb(E.RouteResult(ran=True, verified=False, outcome="not_verified"))
    for _ in range(2):
        res = E.run(plan, _ctx(client, verbs={"app_volume": mixer}))
        assert res.outcome == "handed_over" and res.handoff is not None
    again = _plan(client, demo, "demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"])
    assert isinstance(again, Handoff) and again.skipped == {"A:in_app:uia_invoke": "when",
                                                            "A:mixer:app_volume": "disabled",
                                                            "B:steps:uia_steps": "untrusted"}
    assert R.trust(client.store, vol.key)["A:mixer:app_volume"].failed == 2


def test_done_is_said_only_when_the_check_returned_true_property(client, demo) -> None:
    rng = random.Random(1009)
    vol = demo.get("demo.volume")
    R.import_evidence(client.store, vol.key, vol.routes[2], ok=1, source="procedure:72")
    for trial in range(250):
        verbs = {name: Verb(E.RouteResult(ran=rng.choice([True, False, None]),
                                          verified=rng.choice([True, False, None]),
                                          already=rng.random() < 0.1, outcome=rng.choice(["sent", "x"])))
                 for name in ("uia_invoke", "app_volume", "uia_steps")}
        level = Probe(*[rng.choice([True, False, None]) for _ in range(rng.randint(1, 6))])
        slots = {"level": rng.randint(0, 100), "control": rng.choice(["in_app", "mixer"])}
        plan = _plan(client, demo, "demo.volume", slots, ["invented"])
        if not isinstance(plan, Plan):
            continue
        res = E.run(plan, _ctx(client, verbs=verbs, probes={"level": level}))
        seen_true = True in level.reads or any(v.result.verified is True and v.calls for v in verbs.values())
        if res.done:
            assert seen_true, (trial, res.outcome, level.reads)
            assert res.verified is True
        if res.outcome == "done":
            assert res.verified is True and res.done_line
        if res.outcome != "done":
            assert res.done_line == "" or res.outcome == "already"
    said = client.store._query("SELECT count(*) FROM skill_runs WHERE outcome = 'done' AND coalesce(checked, 0) != 1")
    assert said[0][0] == 0                                                 # the ledger agrees: done => checked 1


def test_other_device_is_said_plainly_never_done_and_never_counted(client, demo) -> None:
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    other = Verb(E.RouteResult(ran=False, outcome="other_device"))
    res = E.run(plan, _ctx(client, verbs={"uia_invoke": other}))
    assert res.outcome == "other_device" and not res.done and res.say == "Demoplayer is playing on another device, " \
                                                                       "so I left it."
    assert R.trust(client.store, "demo.volume").get("A:in_app:uia_invoke").failed == 0


def test_already_is_checked_and_is_no_fresh_done(client, demo) -> None:
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    res = E.run(plan, _ctx(client, verbs={"uia_invoke": Verb(E.RouteResult(ran=False, already=True,
                                                                           say="It's already at 40."))},
                           probes={"level": Probe(True)}))
    assert res.outcome == "already" and res.done and res.done_line == "It's already at 40."
    assert _rows(client)[-1]["checked"] is None                            # a no-op proves no rung


def test_the_recheck_marks_a_false_done(client, demo) -> None:
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    log: list = []
    res = E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}, probes={"level": Probe(True, False)}, log=log))
    assert res.outcome == "done" and res.recheck_verdict is False
    assert _rows(client)[-1]["recheck"] == 0
    t = R.trust(client.store, "demo.volume")["A:in_app:uia_invoke"]
    assert (t.ok, t.failed) == (0, 1)
    rec = [f for e, f in log if e == "done_recheck"]
    assert rec and rec[0]["result"] == "false_done" and rec[0]["recheck"] == "same_source"


def test_the_recheck_runs_on_a_short_thread_when_live(client, demo) -> None:
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    ctx = _ctx(client, verbs={"uia_invoke": Verb()}, probes={"level": Probe(True)})
    ctx.recheck = "thread"
    res = E.run(plan, ctx)
    assert res.outcome == "done" and isinstance(res.recheck_thread, threading.Thread)
    res.recheck_thread.join(5)
    assert not res.recheck_thread.is_alive() and res.recheck_verdict is True


def test_the_ledger_row_exists_before_the_first_step(client, demo) -> None:
    seen: list = []

    def look(call: E.RouteCall) -> None:
        seen.extend(dict(r) for r in client.store._query("SELECT outcome, app, end_kind FROM skill_runs"))

    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    E.run(plan, _ctx(client, verbs={"uia_invoke": Verb(on_call=look)}, probes={"level": Probe(True)}))
    assert seen == [{"outcome": "running", "app": "demoplayer", "end_kind": "volume_change"}]


# -- D17: nothing reaches a game ------------------------------------------------------------------------------------


def test_a_hand_off_to_the_thinker_with_a_game_in_front_is_held(client, demo) -> None:
    client.store._write(lambda c: c.execute("UPDATE skill_routes SET disabled_at = ?", (time.time(),)))
    handoff = _plan(client, demo, "demo.volume", {"level": 40, "control": "mixer"}, ["forty"])
    assert isinstance(handoff, Handoff)
    res = E.run(handoff, _ctx(client, game=lambda: "invented-game"))
    assert res.outcome == "held_game" and res.handoff is None and res.say.endswith("after the round?")


def test_a_card_run_after_a_game_came_to_the_front_is_held(client, demo) -> None:
    calls = {"n": 0}

    def game() -> str:          # the game came to the front after the card's skill was resolved
        calls["n"] += 1
        return "invented-game"

    plan = _plan(client, demo, "demo.home", {}, ["open demo home"])        # resolved with no game in front
    uri = Verb()
    res = E.run(plan, _ctx(client, verbs={"open_uri": uri}, game=game, origin="card"))
    assert calls["n"] >= 1
    assert uri.calls == [] and res.outcome == "held_game"                 # re-read right before the focus step


def test_every_focus_step_rereads_the_game_check(client, demo) -> None:
    calls = {"n": 0}

    def game() -> str:
        calls["n"] += 1
        return ""

    plan = _plan(client, demo, "demo.home", {}, ["open demo home"])
    res = E.run(plan, _ctx(client, verbs={"open_uri": Verb()}, game=game, probes={"foreground": Probe(True)}))
    assert res.outcome == "done" and res.focus_taken and calls["n"] == 1
    calls["n"] = 0
    always = _plan(client, demo, "demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"])
    E.run(always, _ctx(client, verbs={"app_volume": Verb(E.RouteResult(ran=True, verified=True, outcome="set"))},
                       game=game))
    assert calls["n"] == 0                                    # a no-focus rung whose data says "always" reads nothing
    states = _plan(client, demo, "demo.volume", {"level": 40, "control": "in_app"}, ["forty with in_app"])
    assert [r.key for r in states.routes] == ["A:in_app:uia_invoke"]
    E.run(states, _ctx(client, verbs={"uia_invoke": Verb()}, game=game, probes={"level": Probe(True)}))
    assert calls["n"] == 1                                    # a "states" rung re-reads it too (review M5)


def test_a_route_the_backstop_refused_is_held_never_handed_over(client, demo) -> None:
    plan = _plan(client, demo, "demo.home", {}, ["open demo home"])
    refused = Verb(E.RouteResult(ran=False, held="game", outcome="refused"))
    games = iter(["", "invented-game"])
    res = E.run(plan, _ctx(client, verbs={"open_uri": refused}, game=lambda: next(games, "invented-game")))
    assert res.outcome == "held_game" and res.handoff is None


def test_open_uri_goes_through_launch_open_url_with_no_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from yuki.actions import launch

    seen: list = []

    def fake_open_url(url: str, *, app: str | None = None, **kw: Any) -> Any:
        seen.append((url, app))
        return SimpleNamespace(ok=False, details={"refused": "game", "url": url})

    monkeypatch.setattr(launch, "open_url", fake_open_url)
    call = E.RouteCall(C.load([FIXTURES]).get("demo.home"), C.load([FIXTURES]).get("demo.home").routes[0],
                       {"uri": "demoplayer://home"}, {}, E.RunContext(), time.monotonic() + 5)
    got = E.v_open_uri(call)
    assert seen == [("demoplayer://home", None)] and got.held == "game" and got.ran is False


def test_the_slider_verb_never_passes_a_key_a_pointer_or_a_hotkey(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import yuki.actions.slider as slider
    from yuki.skills import uia_route

    seen: list = []

    def fake_set_slider(hwnd: int, element: dict, **kw: Any) -> Any:
        seen.append(kw)
        return SimpleNamespace(ok=False, details={"verified": False})

    monkeypatch.setattr(slider, "set_slider", fake_set_slider)
    monkeypatch.setattr(uia_route, "app_window", lambda process: 4242)
    demo = C.load([FIXTURES])
    route = C.Route(rung="A", via="slider", variant="in_app", needs_focus=False, game="always")
    got = E.v_slider(E.RouteCall(demo.get("demo.volume"), route, {"window": "DemoPlayer.exe", "name": "Demo volume",
                                                                  "to": 40}, {}, E.RunContext(), time.monotonic() + 5))
    assert seen and seen[0]["press"] is None and seen[0]["hover"] is None and seen[0]["hotkey"] is None
    assert got.verified is False


def test_a_pattern_call_on_a_games_own_window_is_refused_but_not_one_beside_a_game(monkeypatch) -> None:
    from yuki.memory import gamegate
    from yuki.skills import uia_route

    asked: list = []

    def refuse(action: str, *, hwnd: int | None = None, front_ok: bool = False, **kw: Any) -> str | None:
        asked.append((action, hwnd, front_ok))
        return "that window belongs to a game" if hwnd == 666 else None

    monkeypatch.setattr(gamegate, "refuse_input", refuse)
    monkeypatch.setattr(uia_route, "_uia", lambda job, budget, fn=None: ({"found": uia_route.Found("missing")}, False))
    assert uia_route.press(666, role="Button", name="Play").outcome == "refused"
    assert uia_route.press(42, role="Button", name="Play").outcome == "missing"
    assert asked == [("uia_pattern", 666, True), ("uia_pattern", 42, True)]


def test_thinker_tools_never_send_input_or_focus_with_a_game_in_front(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.agent.tools import Dispatcher
    from yuki.memory import gamegate

    class Desk:
        def __init__(self) -> None:
            self.calls: list = []

        def __getattr__(self, name: str) -> Any:
            def record(*a: Any, **k: Any) -> Any:
                self.calls.append(name)
                raise RuntimeError("the desk was reached")
            return record

    monkeypatch.setattr(gamegate, "game_in_front", lambda: "invented-game")
    monkeypatch.setattr(gamegate, "window_game", lambda hwnd: "invented-game" if hwnd == 7 else "")
    desk = Desk()
    d = Dispatcher(desk)
    d.game_guard = True
    for name, args in (("click", {"x": 1, "y": 2}), ("hotkey", {"keys": ["ctrl", "k"]}), ("press", {"key": "enter"}),
                       ("type_text", {"text": "x"}), ("focus_window", {"hwnd": 5}), ("launch_app", {"name": "x"}),
                       ("open_url", {"url": "https://example.invalid"}), ("run_powershell", {"command": "dir"}),
                       ("set_slider", {"element": 1, "to": 5}), ("hover", {"x": 1, "y": 1}),
                       ("scroll", {"direction": "down"}), ("tab", {"hwnd": 7, "action": "select", "tab": "1"})):
        out = d.dispatch(name, args)
        assert out.ok is False and out.payload.get("refused") == "game", name
    assert set(desk.calls) <= {"window_exists"}                            # reads only: nothing sent, nothing focused
    # no input, no focus: the mixer reaches the desk as before
    d.dispatch("app_volume", {"app": "invented.exe", "level": 10})
    assert "app_volume" in desk.calls


# -- what leaves the engine ------------------------------------------------------------------------------------------


def test_logs_are_content_free(client, demo) -> None:
    log: list = []
    plan = _plan(client, demo, "demo.play", {"list": SECRET_WORDS[2]}, [f"play {SECRET_WORDS[2]} for {SECRET_WORDS[0]}"])
    E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}, probes={"uia_name": Probe(True),
                                                                    "media_session": Probe(True),
                                                                    "audio_active": Probe(True)},
                     log=log, words=[f"play {SECRET_WORDS[2]} for {SECRET_WORDS[1]}"]))
    text = repr(log) + repr(_rows(client, "demo.play"))
    assert log and not any(w.casefold() in text.casefold() for w in SECRET_WORDS)
    events = {e for e, _f in log}
    assert {"skill_resolve", "route_try", "done_check", "done_recheck", "skill_run"} <= events


def test_the_pinned_skill_is_small_and_carries_a_machine_check(demo) -> None:
    vol = demo.get("demo.volume")
    tried = [{"rung": "A", "via": "uia_invoke", "outcome": "missing" * 30}] * 10
    pin = E.pinned(vol, {"level": 40.0, "control": "in_app"}, {"level": "words", "control": "default"}, tried)
    import json

    assert len(json.dumps(pin, ensure_ascii=False)) <= E.PINNED_CHARS
    assert pin["done"]["probe"] == "level" and pin["done"]["value"] == 40.0
    assert pin["slots"]["level"] == {"value": 40.0, "from": "words"}
    from yuki.agent.frame import PINNED_CHARS, pinned_lines

    lines = pinned_lines(pin)
    assert lines[0].startswith("The user's usual way to do this (skill demo.volume)")
    assert sum(len(x) + 1 for x in lines) <= PINNED_CHARS


def test_run_for_voice_gives_the_hands_their_facts(monkeypatch: pytest.MonkeyPatch, client, demo) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", Verb())
    from yuki.skills import donecheck

    monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", Probe(True))
    hands = SimpleNamespace(memory=client)
    ok, report, facts = E.run_for_voice(hands, {"skill": "demo.volume", "slots": {"level": 40}},
                                        ["Demo Player to forty"], game="")
    assert ok is True and facts["verified"] is True
    assert facts["done_line"] == "Demo Player at 40% with its in_app control."
    assert facts["skill"] == "demo.volume" and facts["handoff_skill"] is None and report.startswith("Done and checked")
    ok, _r, facts = E.run_for_voice(hands, {"skill": "demo.home", "slots": {}}, ["open demo home"],
                                    game="invented-game")
    assert ok is False and facts["held"] == "game" and facts["refused"] == "game" and facts["settled"]


def test_a_broken_verb_is_an_outcome_never_a_crash(client, demo) -> None:
    def boom(call: E.RouteCall) -> E.RouteResult:
        raise ValueError("invented failure")

    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    res = E.run(plan, _ctx(client, verbs={"uia_invoke": boom}, probes={"level": Probe(None)}))
    assert res.outcome in ("unverified", "handed_over", "failed") and not res.done


def test_the_off_switch_runs_nothing(client, demo, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    verb = Verb()
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", verb)
    ok, _r, facts = E.run_for_voice(SimpleNamespace(memory=client), {"skill": "demo.volume", "slots": {"level": 4}},
                                    ["four"], game="", settings=SimpleNamespace(skills=False))
    assert ok is False and facts["outcome"] == "refused" and verb.calls == []


def test_the_logs_keep_their_code_words(client, demo) -> None:
    log: list = []
    plan = _plan(client, demo, "demo.volume", {"level": 40}, ["forty"])
    E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}, probes={"level": Probe(True)}, log=log))
    run = next(f for e, f in log if e == "skill_run")
    assert run["skill"] == "demo.volume" and run["route"] == "a:in_app:uia_invoke" and run["result"] == "done"
    assert run["lane"] == "voice" and run["rung"] == "a"
    check = next(f for e, f in log if e == "done_check")
    assert check["check"] == "level" and check["result"] == "done"
    resolved = next(f for e, f in log if e == "skill_resolve")
    assert resolved["slot_source"] == ["default", "words"]


def test_a_contradicted_rung_is_counted_once(client, demo) -> None:
    vol = demo.get("demo.volume")
    plan = _plan(client, demo, "demo.volume", {"level": 40, "control": "mixer"}, ["forty on the mixer"])
    E.run(plan, _ctx(client, verbs={"app_volume": Verb(E.RouteResult(ran=True, verified=False))}))
    assert R.trust(client.store, vol.key)["A:mixer:app_volume"].failed == 1
    assert len(_rows(client)) == 1 and _rows(client)[0]["rung"] is None   # the run's row names no rung


# -- review round 1 ----------------------------------------------------------------------------------------------------


def _load_one(tmp_path: Path, text: str) -> C.Loaded:
    folder = tmp_path / "conn"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "one.toml").write_text(text, encoding="utf-8")
    C.clear_cache()
    loaded = C.load([folder])
    assert loaded.errors == (), loaded.errors
    return loaded


def _client_for(tmp_path: Path, loaded: C.Loaded) -> MemoryClient:
    c = MemoryClient(Store.open(tmp_path / "review.db"))
    R.seed(c.store, loaded)
    return c


MIXER = """
[connection]
app = "audio"

[skill."audio.level"]
end = "volume_change"
kind = "change"
menu = "set an app's level in the mixer, or the whole PC's"
done_line = "At {level}%."

[[skill."audio.level".slot]]
name = "target"
type = "app"
options = ["inventedplayer", "chatapp"]

[[skill."audio.level".slot]]
name = "level"
type = "number"
min = 0
max = 100
required = true

[[skill."audio.level".route]]
rung = "A"
variant = "mixer"
via = "app_volume"
when = { slot = "target", present = true }
args = { app = "{target}", level = "{level}" }
recheck = { probe = "level", target = "{target}", value = "{level}", after_s = 1 }

[[skill."audio.level".route]]
rung = "A"
variant = "whole"
via = "endpoint_volume"
when = { slot = "target", present = false }
args = { level = "{level}" }
recheck = { probe = "level", target = "system", value = "{level}", after_s = 1 }
"""


def test_the_run_records_the_app_it_acted_on_so_its_moment_is_yukis(tmp_path: Path) -> None:
    """Review M2 (D18): a mixer level set on another app is that app's moment; the run's row names the acted app
    (from t0 when the first rung's app is known, and at the finish), so fMoments' attribute() stamps by='yuki'."""
    from yuki.memory.moments import Moment, attribute, skill_runs_between
    from yuki.memory.volumewatch import MASTER

    loaded = _load_one(tmp_path, MIXER)
    client = _client_for(tmp_path, loaded)
    try:
        seen: list = []

        def look(call: E.RouteCall) -> None:
            seen.extend(r[0] for r in client.store._query("SELECT app FROM skill_runs WHERE outcome = 'running'"))

        set_ok = E.RouteResult(ran=True, verified=True, outcome="set", value=40.0)
        plan = resolve("audio.level", {"target": "inventedplayer", "level": 40}, ["inventedplayer to 40"],
                       store=client.store, loaded=loaded, live=Live())
        res = E.run(plan, _ctx(client, verbs={"app_volume": Verb(set_ok, on_call=look)}))
        assert res.outcome == "done" and seen == ["inventedplayer"]          # named before the rung acted
        row = dict(client.store._query("SELECT app, t0, t_done FROM skill_runs ORDER BY id DESC LIMIT 1")[0])
        assert row["app"] == "inventedplayer"
        runs = skill_runs_between(client.store, row["t0"] - 1, row["t_done"] + 5)
        moment = Moment(kind="volume_change", at=row["t0"] + 0.5, source="volume", app="inventedplayer",
                        value=40.0, means="session")
        assert attribute(moment, runs, now=row["t_done"] + 1).by == "yuki"   # not "your habit"
        other = Moment(kind="volume_change", at=row["t0"] + 0.5, source="volume", app="chatapp", value=40.0,
                       means="session")
        assert attribute(other, runs, now=row["t_done"] + 1).by == "user"
        # the whole PC's level is the volume watcher's "master"
        whole = resolve("audio.level", {"level": 30}, ["thirty"], store=client.store, loaded=loaded, live=Live())
        E.run(whole, _ctx(client, verbs={"endpoint_volume": Verb(set_ok)}))
        assert client.store._query("SELECT app FROM skill_runs ORDER BY id DESC LIMIT 1")[0][0] == MASTER
        # a verb that says which app it acted on (a driver) wins
        told = E.RouteResult(ran=True, verified=True, outcome="set", app="Discord.exe")
        E.run(plan, _ctx(client, verbs={"app_volume": Verb(told)}))
        assert client.store._query("SELECT app FROM skill_runs ORDER BY id DESC LIMIT 1")[0][0] == "discord"
    finally:
        client.close()


CHAT = """
[connection]
app = "inventedchat"

[skill."invented.say"]
end = "message_sent"
kind = "change"
menu = "send a line in Invented Chat"
done_line = "Sent."
sends = true

[[skill."invented.say".slot]]
name = "text"
type = "text"
required = true

[[skill."invented.say".route]]
rung = "A"
variant = "box"
via = "uia_invoke"
args = { window = "InventedChat.exe", role = "Button", name = "Send" }
done = { probe = "message_sent", app = "inventedchat", text = "{text}", within_s = 1 }

[[skill."invented.say".route]]
rung = "A"
variant = "again"
via = "uia_invoke"
args = { window = "InventedChat.exe", role = "Button", name = "Send now" }
done = { probe = "message_sent", app = "inventedchat", text = "{text}", within_s = 1 }
"""


@pytest.mark.parametrize("verdict,outcome", [(False, "failed"), (None, "unverified")])
def test_a_sending_skill_never_tries_another_rung_after_one_acted(tmp_path: Path, verdict: Any, outcome: str) -> None:
    """Review M3: after a rung of a skill that sends acted, neither another acting rung nor the thinker is tried
    (a second send); "couldn't confirm it landed" stays None."""
    loaded = _load_one(tmp_path, CHAT)
    client = _client_for(tmp_path, loaded)
    try:
        plan = resolve("invented.say", {"text": "see you at seven"}, ["tell them see you at seven"],
                       store=client.store, loaded=loaded, live=Live())
        assert isinstance(plan, Plan) and len(plan.routes) == 2
        press = Verb(E.RouteResult(ran=True, outcome="pressed"))
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": press}, probes={"message_sent": Probe(verdict)}))
        assert res.outcome == outcome and len(press.calls) == 1 and res.handoff is None and not res.done
        if verdict is False:
            assert res.say == E.LINE_SEND_FAILED
        row = dict(client.store._query("SELECT outcome, checked, rung, variant FROM skill_runs")[0])
        assert (row["outcome"], row["variant"]) == (outcome, "box")
    finally:
        client.close()


def test_a_rung_whose_data_says_no_retry_after_acting_stops_too(tmp_path: Path) -> None:
    loaded = _load_one(tmp_path, CHAT.replace("sends = true\n", "").replace(
        'variant = "box"\n', 'variant = "box"\nretry_after_acting = false\n'))
    client = _client_for(tmp_path, loaded)
    try:
        assert not loaded.skills[0].sends and not loaded.skills[0].routes[0].retry_after_acting
        plan = resolve("invented.say", {"text": "see you at seven"}, ["see you at seven"], store=client.store,
                       loaded=loaded, live=Live())
        press = Verb(E.RouteResult(ran=True, outcome="pressed"))
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": press}, probes={"message_sent": Probe(False)}))
        assert res.outcome == "failed" and len(press.calls) == 1 and res.say == E.LINE_FAILED
        # a rung that did not act still passes to the next one
        idle = Verb(E.RouteResult(ran=False, outcome="missing"))
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": idle}, probes={"message_sent": Probe(False)}))
        assert len(idle.calls) == 2
    finally:
        client.close()


VOICE = """
[connection]
app = "inventedvoice"

[skill."invented.join"]
end = "call_join"
kind = "change"
menu = "join the invented voice room"
done_line = "You're in."

[[skill."invented.join".route]]
rung = "A"
via = "uia_invoke"
args = { window = "InventedVoice.exe", role = "Button", name = "Join" }
done = { probe = "mic_held", process = "InventedVoice.exe", within_s = 0.5 }
"""


def _mic_script(monkeypatch: pytest.MonkeyPatch, *states: bool) -> list:
    from yuki.skills import donecheck as D

    answers = list(states)
    reads: list = []

    def held(want: str) -> tuple:
        v = answers.pop(0) if len(answers) > 1 else answers[0]
        reads.append(v)
        return v, 1

    monkeypatch.setattr(D, "_mic_held_by", held)
    monkeypatch.setitem(D.PROBE_FUNCS, "mic_held", D.probe_mic_held)
    monkeypatch.setitem(D.BASELINES, "mic_held", D.baseline_mic_held)
    return reads


def test_mic_held_needs_an_onset_after_the_run_began(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review M4: the microphone already held before the rung acted (another channel) is not this run's doing."""
    loaded = _load_one(tmp_path, VOICE)
    client = _client_for(tmp_path, loaded)
    try:
        plan = resolve("invented.join", {}, ["join"], store=client.store, loaded=loaded, live=Live())
        reads = _mic_script(monkeypatch, True)                     # held before, still held
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}))
        assert res.outcome == "unverified" and not res.done and reads[0] is True
        _mic_script(monkeypatch, False, True)                      # taken after the run began
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}))
        assert res.outcome == "done" and res.done
        _mic_script(monkeypatch, False, False)                     # never taken: contradicted
        res = E.run(plan, _ctx(client, verbs={"uia_invoke": Verb()}))
        assert res.outcome == "handed_over"
        # the hand-off carries the state read right before the thinker acts; its finish gate compares with it
        assert res.handoff["baseline"] == {"mic_held:inventedvoice.exe": False}
        from yuki.agent.finish import skill_gate

        _mic_script(monkeypatch, True)
        assert skill_gate(res.handoff, since=0.0, bounces=0)[0] == "ok"
        assert skill_gate({**res.handoff, "baseline": {"mic_held:inventedvoice.exe": True}}, since=0.0,
                          bounces=0)[0] == "hold"
        assert skill_gate({k: v for k, v in res.handoff.items() if k != "baseline"}, since=0.0,
                          bounces=0)[0] == "hold"
    finally:
        client.close()


LADDER = """
[connection]
app = "demoplayer"

[skill."demo.loud"]
end = "volume_change"
kind = "change"
menu = "set Demo Player's level"
done_line = "At {level}%."

[[skill."demo.loud".slot]]
name = "level"
type = "number"
min = 0
max = 100
required = true

[[skill."demo.loud".route]]
rung = "A"
variant = "mixer"
via = "app_volume"
args = { app = "DemoPlayer.exe", level = "{level}" }
recheck = { probe = "level", target = "DemoPlayer.exe", value = "{level}", after_s = 1 }

[[skill."demo.loud".route]]
rung = "A"
variant = "never"
via = "uia_invoke"
args = { window = "DemoPlayer.exe", role = "Slider", name = "Demo volume", pattern = "range", value = "{level}" }
game = "never"
done = { probe = "level", target = "DemoPlayer.exe", value = "{level}", within_s = 1 }

[[skill."demo.loud".route]]
rung = "A"
variant = "states"
via = "slider"
args = { window = "DemoPlayer.exe", name = "Demo volume", to = "{level}" }
game = "states"
game_states = ["covered"]
"""


def test_a_game_that_came_up_during_an_earlier_rung_stops_a_never_rung(tmp_path: Path) -> None:
    """Review M5: the route's game rule is applied again with a fresh read before every rung, not only at resolve."""
    loaded = _load_one(tmp_path, LADDER)
    client = _client_for(tmp_path, loaded)
    try:
        plan = resolve("demo.loud", {"level": 40}, ["forty"], store=client.store, loaded=loaded, live=Live())
        assert [r.variant for r in plan.routes] == ["mixer", "never", "states"]
        up = {"game": ""}
        mixer = Verb(E.RouteResult(ran=True, verified=False, outcome="not_verified"),
                     on_call=lambda call: up.update(game="invented-game"))
        invoke, slider = Verb(), Verb(E.RouteResult(ran=True, verified=True, outcome="set"))
        ctx = _ctx(client, verbs={"app_volume": mixer, "uia_invoke": invoke, "slider": slider},
                   game=lambda: up["game"])
        ctx.window_state = lambda app: "front"                   # not a proven state
        res = E.run(plan, ctx)
        assert invoke.calls == [] and slider.calls == [] and res.outcome == "held_game" and res.handoff is None
        assert [t["outcome"] for t in res.tried] == ["contradicted", "game", "game"]
        # in a proven state the "states" rung runs mid-game
        up["game"] = ""
        ctx = _ctx(client, verbs={"app_volume": mixer, "uia_invoke": invoke, "slider": slider},
                   game=lambda: up["game"])
        ctx.window_state = lambda app: "covered"
        res = E.run(plan, ctx)
        assert invoke.calls == [] and len(slider.calls) == 1 and res.outcome == "done"
    finally:
        client.close()


def test_in_call_is_read_for_every_origin_and_already_has_its_own_words(tmp_path: Path) -> None:
    """Review L7: a thinker-origin run reads memory's open calls when a slot asks in a call; an "already" no-op is
    reported as nothing to change, never as a fresh done."""
    skill = C.Skill(key="x.y", app="x", end="volume_change", kind="change", menu="m", done_line="d",
                    slots=(C.Slot(name="target", type="app", ask_in_call=True),),
                    routes=(C.Route(rung="A", via="app_volume", needs_focus=False, game="always"),))

    class Mem:
        store = None

        def __init__(self, calls: list) -> None:
            self.calls = calls

        def current_calls(self) -> list:
            return self.calls

    assert E._live_now("", None, media=False, skill=skill, memory=Mem([{"app": "chatapp"}])).in_call is True
    assert E._live_now("", None, media=False, skill=skill, memory=Mem([])).in_call is False
    assert E._live_now("", SimpleHands(False), media=False, skill=skill, memory=Mem([{"app": "c"}])).in_call is False
    plain = C.Skill(**{**skill.__dict__, "slots": (C.Slot(name="target", type="app"),)})
    assert E._live_now("", None, media=False, skill=plain, memory=Mem([{"app": "c"}])).in_call is False   # not read
    already = E.SkillResult(skill="x.y", outcome="already", verified=True, done_line="It's already at 40.")
    assert already.report().startswith("Nothing to change") and "Done and checked" not in already.report()


class SimpleHands:
    def __init__(self, in_call: bool) -> None:
        self.in_call = in_call
