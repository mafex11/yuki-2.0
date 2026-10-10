"""sCore: the thinker side and the plumbing (map-skills S3.4-S3.5; PLAN.md D13, D19; B.2 "the pinned bounce stops at
2"), the memory API calls, intake, and the footprint bars (C.5: marginal import, no threads, tracemalloc).

Scripted fake model clients and stub desks only; temp databases only; invented names only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import registry as R

FIXTURES = Path(__file__).with_name("skills_fixtures")
ROOT = Path(__file__).resolve().parents[1]
PIN = {"key": "demo.volume", "app": "demoplayer", "goal": "set Demo Player's own volume",
       "slots": {"level": {"value": 40.0, "from": "words"}}, "done_words": "Demo Player at 40%.",
       "done": {"probe": "level", "target": "DemoPlayer.exe", "value": 40.0, "within_s": 1.0},
       "tried": [{"rung": "A", "via": "uia_invoke", "why": "missing"}]}


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


# -- the task frame's pinned block -----------------------------------------------------------------------------------


def test_the_pin_survives_whole_and_its_apps_paths_and_pitfalls_are_left_out() -> None:
    from yuki.agent import frame

    practice = {
        "ways": [{"ref": "pb:1", "kind": "procedure", "intent": "invented", "apps": ["DemoPlayer.exe"], "label": "x",
                  "text": "w" * 400, "id": 1},
                 {"ref": "pb:2", "kind": "procedure", "intent": "other", "apps": ["other.exe"], "label": "x",
                  "text": "v" * 400, "id": 2}],
        "pitfalls": [{"ref": "pb:3", "apps": ["demoplayer"], "intent": "i", "label": "l", "text": "p" * 400, "id": 3}],
        "go_tos": [{"app": "demoplayer", "elements": [{"label": "Button \"Demo louder\"", "n": 9}]}],
    }
    facts = {"named": [], "routes": {}}
    big = frame.build(facts, practice=practice, lately=["x" * 300] * 8, chars=frame.FRAME_CHARS, pinned=PIN)
    lines = frame.pinned_lines(PIN)
    assert all(line in big.text for line in lines)                       # never cut
    assert big.text.index(lines[0]) == len(frame.LABEL) + 1              # first after the label
    assert "w" * 50 not in big.text and "p" * 50 not in big.text and "Demo louder" not in big.text
    assert big.skill == "demo.volume"
    tiny = frame.build(facts, practice=practice, chars=700, pinned=PIN)
    assert all(line in tiny.text for line in lines)                       # the others give way, never the pin
    plain = frame.build(facts, practice=practice, chars=frame.FRAME_CHARS)
    assert "w" * 50 in plain.text and plain.skill is None                 # no pin: as before


# -- the finish gate -------------------------------------------------------------------------------------------------


def test_the_skill_gate_holds_twice_then_ends_as_couldnt_confirm() -> None:
    from yuki.agent.finish import SKILL_BOUNCES, SKILL_UNCONFIRMED, skill_gate

    never = SimpleNamespace(verdict=False, evidence={"status": "stub"})
    assert skill_gate(PIN, since=0, bounces=0, check=lambda d, since: never)[0] == "hold"
    assert skill_gate(PIN, since=0, bounces=SKILL_BOUNCES - 1, check=lambda d, since: never)[0] == "hold"
    assert skill_gate(PIN, since=0, bounces=SKILL_BOUNCES, check=lambda d, since: never) == ("unconfirmed",
                                                                                              SKILL_UNCONFIRMED)
    seen = SimpleNamespace(verdict=True, evidence={})
    assert skill_gate(PIN, since=0, bounces=0, check=lambda d, since: seen) == ("ok", None)
    assert skill_gate({**PIN, "done": None}, since=0, bounces=0) == ("ok", None)   # no machine check: the trace decides
    assert skill_gate(None, since=0, bounces=0) == ("ok", None)


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _response(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class Scripted:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        return self.responses.pop(0)


class Desk:
    def get_desktop_overview(self) -> dict:
        return {"windows": [], "foreground_hwnd": 0}

    def format_overview(self, overview: Any) -> str:
        return "(no windows)"

    def activity_facts(self) -> dict:
        return {}


def _agent(client: Any, tmp: Path):
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings
    from yuki.log.events import SessionLogger

    s = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    return Agent(s, SessionLogger(s.sessions_dir, console=None), client=client, backend=Desk(), prewarm=False,
                 record_requests=False, memory=memory)


def _done(n: int) -> SimpleNamespace:
    return _response(_block("tool_use", id=f"t{n}", name="done", input={"message": "Set it to 40."}))


def test_a_pinned_done_is_bounced_at_most_twice_then_said_as_couldnt_confirm(monkeypatch, tmp_path: Path) -> None:
    from yuki.log.events import Final
    from yuki.skills import donecheck

    reads = {"n": 0}

    def never(done: Any, *, since: float, **kw: Any) -> Any:
        reads["n"] += 1
        return donecheck.Waited(False, {"status": "stub"}, 1, 1.0)

    monkeypatch.setattr(donecheck, "check", never)
    client = Scripted([_done(1), _done(2), _done(3)])
    agent = _agent(client, tmp_path)
    events = list(agent.run("set Demo Player to 40", skill=dict(PIN)))
    final = events[-1]
    assert isinstance(final, Final) and reads["n"] == 3 and len(client.calls) == 3
    assert final.outcome["level"] == "unverified"                          # never "done" past the gate
    # review L4: nothing acted and the done gave no claim, yet "Set it to 40." never stands
    from yuki.agent.finish import LINE_SKILL_UNCONFIRMED

    assert final.text == LINE_SKILL_UNCONFIRMED
    notes = json.dumps(client.calls[1]["messages"], default=str)
    assert "not done yet" in notes


def test_a_pinned_done_the_check_sees_ends_at_once(monkeypatch, tmp_path: Path) -> None:
    from yuki.log.events import Final
    from yuki.skills import donecheck

    monkeypatch.setattr(donecheck, "check", lambda done, *, since, **kw: donecheck.Waited(True, {}, 1, 1.0))
    client = Scripted([_done(1)])
    events = list(_agent(client, tmp_path).run("set Demo Player to 40", skill=dict(PIN)))
    assert isinstance(events[-1], Final) and len(client.calls) == 1


def test_the_pin_reaches_the_frame_and_without_one_nothing_changes(monkeypatch, tmp_path: Path) -> None:
    from yuki.agent import frame

    seen: list = []
    real = frame.build

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(k.get("pinned"))
        return real(*a, **k)

    monkeypatch.setattr(frame, "build", spy)
    list(_agent(Scripted([_done(1)]), tmp_path).run("hello"))
    assert all(p is None for p in seen)


# -- the tools ---------------------------------------------------------------------------------------------------------


def test_run_skill_is_in_the_block_only_with_skills_and_the_volume_skill_drops_app_volume() -> None:
    from yuki.agent import tools as T

    plain = [t["name"] for t in T.tool_params(cacheable=False)]
    assert "run_skill" not in plain and "app_volume" in plain
    with_demo = T.tool_params(cacheable=False, skill_keys=["demo.volume", "demo.home"], skills_menu="- demo...")
    names = [t["name"] for t in with_demo]
    assert "run_skill" in names and "app_volume" in names
    tool = next(t for t in with_demo if t["name"] == "run_skill")
    assert tool["input_schema"]["properties"]["skill"]["enum"] == ["demo.volume", "demo.home"]
    assert tool["description"].endswith("- demo...")
    with_volume = [t["name"] for t in T.tool_params(cacheable=False, skill_keys=["volume"], skills_menu="- volume")]
    assert "app_volume" not in with_volume
    again = T.tool_params(cacheable=False, skill_keys=["demo.volume", "demo.home"], skills_menu="- demo...")
    assert json.dumps(again) == json.dumps(with_demo)                     # byte-stable for the cache
    assert not T.tool_traits("run_skill", {"skill": "volume", "slots": {"level": 4}}).reaches
    assert "run_skill" in T.ACTION_TOOL_NAMES


SENDING = """
[connection]
app = "inventedchat"

[skill."invented.note"]
end = "message_sent"
kind = "change"
menu = "send a note in Invented Chat"
done_line = "Sent."
sends_when = { slot = "mode", in = ["now"] }

[[skill."invented.note".slot]]
name = "words"
type = "text"

[[skill."invented.note".slot]]
name = "mode"
type = "enum"
options = ["now", "draft"]
default = "now"

[[skill."invented.note".route]]
rung = "A"
via = "uia_invoke"
args = { window = "InventedChat.exe", role = "Button", name = "Send" }
done = { probe = "message_sent", app = "inventedchat", text = "{words}" }
recheck = { probe = "message_sent", app = "inventedchat", text = "{words}", after_s = 2 }
"""


def test_a_send_is_judged_from_the_skill_data_not_slot_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review L3: ``sends`` / ``sends_when`` in the connection file decide whether ``run_skill`` is judged as a send
    at the thinker's authorizer (rule 8), whatever the slots are called."""
    from yuki.agent import tools as T

    (tmp_path / "chat.toml").write_text(SENDING, encoding="utf-8")
    monkeypatch.setattr(C, "CONNECTIONS_DIR", tmp_path)
    C.clear_cache()
    try:
        skill = C.load().get("invented.note")
        assert skill is not None and skill.sends and skill.sends_when == ({"slot": "mode", "in": ["now"]},)
        assert T.tool_traits("run_skill", {"skill": "invented.note", "slots": {"words": "hi", "mode": "now"}}).reaches
        # a slot the model left out may still be filled with a sending value: judged as a send
        assert T.tool_traits("run_skill", {"skill": "invented.note", "slots": {"words": "hi"}}).reaches
        assert not T.tool_traits("run_skill", {"skill": "invented.note",
                                               "slots": {"words": "hi", "mode": "draft"}}).reaches
        # the old slot-name guess is gone: an unknown skill with "text"/"send" slots is not judged as a send
        assert not T.tool_traits("run_skill", {"skill": "message", "slots": {"text": "hi", "send": "send"}}).reaches
        assert C.sends_with(skill, {"words": "hi", "mode": "now"}) and not C.sends_with(skill, {"mode": "draft"})
    finally:
        C.clear_cache()


def test_the_thinkers_run_skill_returns_a_checked_outcome_and_pins_a_hand_off(monkeypatch, tmp_path: Path,
                                                                             demo) -> None:
    from yuki.agent.tools import Dispatcher
    from yuki.skills import donecheck
    from yuki.skills import engine as E

    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    try:
        monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", lambda call: E.RouteResult(ran=True, outcome="pressed"))
        monkeypatch.setitem(donecheck.PROBE_FUNCS, "level", lambda ctx, args: (True, {}))
        monkeypatch.setattr("yuki.memory.gamegate.game_in_front", lambda: "")
        d = Dispatcher(Desk())
        d.skill_context = {"memory": client, "words": ["Demo Player to 40"]}
        out = d.dispatch("run_skill", {"skill": "demo.volume", "slots": {"level": 40}})
        assert out.ok and out.payload["details"]["verified"] is True and out.payload["outcome"] == "done"
        monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke", lambda call: E.RouteResult(ran=False, outcome="missing"))
        out = d.dispatch("run_skill", {"skill": "demo.volume", "slots": {"level": 40}})
        assert out.ok and out.payload["outcome"] == "handed_over" and out.payload["handoff"]["key"] == "demo.volume"
        assert "The user's usual way to do this (skill demo.volume)" in out.content[0]["text"]
        rows = client.store._query("SELECT count(*) FROM skill_runs WHERE origin = 'thinker'")
        assert rows[0][0] == 2
    finally:
        client.close()


# -- the runtime ---------------------------------------------------------------------------------------------------------


def test_a_pinned_skill_reaches_agent_run_and_survives_a_front_desk_hand_off(tmp_path: Path) -> None:
    from PySide6.QtCore import QCoreApplication

    from yuki.config import Settings
    from yuki.log.events import Final
    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    QCoreApplication.instance() or QCoreApplication([])

    class Agent:
        tool_names = None
        last_summary = None

        def __init__(self) -> None:
            self.kwargs: list[dict] = []
            self.cancelled, self.cancel_reason, self.awaiting_answer = False, "cancelled", False

        def run(self, request: str, **kw: Any) -> Any:
            self.kwargs.append(kw)
            yield Final("Done.")

        def steer(self, text: str) -> None:
            pass

        def take_steers(self) -> list[str]:
            return []

        def cancel(self, reason: str = "cancelled") -> None:
            pass

    worker, desk = Agent(), Agent()
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=worker,
                      front_desk_agent=desk, memory=SimpleNamespace(session_id="t"))
    assert Job(1, "x").skill is None
    rt.worker.enqueue(Job(1, "set Demo Player to 40", None, "voice", skill=dict(PIN)))
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert worker.kwargs[-1]["skill"]["key"] == "demo.volume"
    rt._skills[7] = dict(PIN)                                              # a front-desk request that needed hands
    new_id = rt.queue_for_worker("set Demo Player to 40", origin_id=7, skill=rt._skills.pop(7))
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert new_id and worker.kwargs[-1]["skill"]["key"] == "demo.volume"
    rt.worker.enqueue(Job(9, "hello", None, "text"))
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert "skill" not in worker.kwargs[-1]                                 # none: Agent.run as before


# -- the memory API and intake -------------------------------------------------------------------------------------


def test_the_memory_api_calls(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    try:
        menu = client.skill_menu("talker")
        assert menu.startswith("Skills") and "- demo.volume:" in menu
        assert client.skill_menu("thinker").startswith("- demo.volume:")
        assert client.store._query("SELECT count(*) FROM skills")[0][0] == 3   # seeded on first use
        v1 = client.skill_menu_version()
        assert v1 and client.skill_menu_version() == v1
        key = client.record_skill_run("start", skill="demo.home", origin="voice", t0=time.time())
        assert client.record_skill_run("finish", run_key=key, outcome="done", checked=True)
        assert client.record_skill_run("recheck", run_key=key, agrees=True)
        info = client.skill_inputs("demo.home")
        assert info["known"] and info["routes"]["A::open_uri"]["usable"] and info["offer_ready"] is False
        assert client.keep_usual("demo.volume:control", "mixer", "yes")["ok"]
        assert client.prefs_choose("demo.volume:control")["option"] == "mixer"
        plan = client.skill_resolve("demo.volume", {"level": 30}, ["thirty"])
        assert plan.values["control"] == "mixer"
    finally:
        client.close()


def test_intake_maps_an_old_procedure_onto_a_rung_by_identity(tmp_path: Path, demo) -> None:
    from yuki.memory import practice
    from yuki.skills.intake import procedure_target, route_target, run_intake

    store = Store.open(tmp_path / "memory.db")
    try:
        vol = demo.get("demo.volume")
        assert route_target(vol.routes[2]) == ("button", "demo louder")
        assert procedure_target([{"do": "click", "role": "Button", "name": "Demo louder"}]) == ("button",
                                                                                                "demo louder")
        pb = SimpleNamespace(id=72, run=[{"do": "click", "role": "Button", "name": "Demo Louder"}],
                             apps=["DemoPlayer.exe"], n_ran_ok=3)
        added: list = []
        orig_live, orig_add = practice.live, practice.add_events
        practice.live = lambda s, kinds=None, **k: [pb]
        practice.add_events = lambda s, events, now=None: added.extend(events) or len(events)
        try:
            dry = run_intake(store, loaded=demo)
            assert dry["result"] == "dry_run" and dry["matches"] == [
                {"procedure": 72, "skill": "demo.volume", "route": "B:steps:uia_steps", "ok": 3}]
            assert store._query("SELECT count(*) FROM skills")[0][0] == 0      # a dry run writes nothing
            done = run_intake(store, loaded=demo, apply=True)
            assert done["imported"] == 1 and done["superseded"] == 1
            assert R.trust(store, "demo.volume")["B:steps:uia_steps"].seed_ok == 3
            assert added[0]["type"] == "superseded" and added[0]["refs"] == ["skill:demo.volume"]
        finally:
            practice.live, practice.add_events = orig_live, orig_add
    finally:
        store.close()


# -- footprint (C.5) ---------------------------------------------------------------------------------------------------


def test_importing_the_skills_package_is_cheap_and_starts_no_thread() -> None:
    code = ("import time, threading\n"
            "import yuki.agent.tools, yuki.agent.frame, yuki.agent.finish, yuki.memory.store\n"
            "before = threading.active_count()\n"
            "t = time.perf_counter()\n"
            "import yuki.skills, yuki.skills.engine, yuki.skills.resolve, yuki.skills.menu, yuki.skills.donecheck,"
            " yuki.skills.registry, yuki.skills.prefs, yuki.skills.taste\n"
            "ms = (time.perf_counter() - t) * 1000\n"
            "print(round(ms, 1), threading.active_count() - before)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    ms, threads = out.stdout.split()
    assert float(ms) < 60.0, out.stdout                                    # marginal import < 60 ms
    assert int(threads) == 0


def test_the_menu_and_the_resolver_use_little_memory(tmp_path: Path, demo) -> None:
    from yuki.skills.menu import menu_text
    from yuki.skills.resolve import resolve

    store = Store.open(tmp_path / "memory.db")
    try:
        R.seed(store, demo)
        resolve("demo.volume", {"level": 1}, ["to 1"], store=store, loaded=demo)   # first-use imports aside
        menu_text(store, loaded=demo)
        threads = threading.active_count()
        tracemalloc.start()
        menu_text(store, loaded=demo)
        for i in range(20):
            resolve("demo.volume", {"level": i}, [f"to {i}"], store=store, loaded=demo)
        _now, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 3 * 1024 * 1024 and threading.active_count() == threads
    finally:
        store.close()


def test_the_requests_row_columns_of_a_skill_run(tmp_path: Path) -> None:
    agent = _agent(Scripted([]), tmp_path)
    agent._skill_last, agent._request_skill, agent._skill_unconfirmed = None, None, False
    assert agent._skill_row() == {}
    agent._skill_last = {"skill": "demo.volume", "route": "A:in_app:uia_invoke", "done_check": "done"}
    assert agent._skill_row() == {"skill": "demo.volume", "route": "A:in_app:uia_invoke", "done_check": "done"}
    agent._skill_last, agent._request_skill, agent._skill_unconfirmed = None, dict(PIN), True
    assert agent._skill_row() == {"skill": "demo.volume", "route": "C", "done_check": "unconfirmed"}


def test_the_thinker_menu_is_byte_stable_through_the_day(tmp_path: Path, demo) -> None:
    """Review L2: the thinker's run_skill description lists taste candidates most seen first (an order that changes
    only with the nightly recompute), so its cached tool block doesn't change hour by hour; the talker's list stays
    best-at-this-hour first."""
    from datetime import datetime

    from yuki.skills.menu import TALKER, THINKER, menu_text

    store = Store.open(tmp_path / "memory.db")
    try:
        R.seed(store, demo)
        morning, evening = [0.0] * 24, [0.0] * 24
        morning[8], evening[20] = 9.0, 9.0
        rows = [("Invented Dawn", morning, 3.0), ("Invented Dusk", evening, 7.0)]
        for name, hist, n in rows:
            h = json.dumps(hist)
            store._write(lambda c, name=name, h=h, n=n: c.execute(
                "INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                "hist_weekend, n_obs, updated_at) VALUES ('demo_list', ?, ?, ?, 'observed', ?, ?, ?, ?)",
                (store.cipher.digest("taste", name), store._enc(name), store._enc(""), h, h, n, time.time() - 10)))
        at8 = datetime(2026, 10, 7, 8, 30).timestamp()
        at20 = datetime(2026, 10, 7, 20, 30).timestamp()
        think8 = menu_text(store, audience=THINKER, loaded=demo, now=at8)
        assert think8 == menu_text(store, audience=THINKER, loaded=demo, now=at20)
        assert "[list: Invented Dusk | Invented Dawn]" in think8               # most seen first
        talk8 = menu_text(store, audience=TALKER, loaded=demo, now=at8)
        assert "[list: Invented Dawn | Invented Dusk]" in talk8               # the talker's: best at this hour
    finally:
        store.close()
