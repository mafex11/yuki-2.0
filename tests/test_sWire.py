"""sWire: wave S merged onto wave F (the design notes).

What crosses the four S branches and fWire: the packaged app loads all seven skills (the connection and site files are
bundled, the drivers resolve through the import system), the doctor's skills line, one request id for a thinker's
``run_skill`` and a voice skill hand (follow joins a hand to its turn), the router knows the talker's skills, the
thinker's ``run_skill`` honours the per-app "Act" switch and a slow memory, the run header records the skill for the
old learners, and the daily taste seed.

Offline: temp databases, stub verbs and probes, invented names. Nothing reads the desktop.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.api import MemoryClient
from yuki.memory.store import Store
from yuki.skills import connection as C
from yuki.skills import donecheck
from yuki.skills import engine as E
from yuki.skills import registry as R

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("skills_fixtures")
SHIPPED_SKILLS = {"volume", "spotify.playlist", "youtube.home", "youtube.video", "message", "discord.voice",
                  "meet.open"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No verb or live probe reaches the desktop unless a test sets its own; no game in front."""
    for name in list(E.VERB_FUNCS):
        monkeypatch.setitem(E.VERB_FUNCS, name, lambda call: E.RouteResult(ran=False, outcome="no_live_verb_in_tests"))
    for name in list(donecheck.PROBE_FUNCS):
        if name != "read_back":
            monkeypatch.setitem(donecheck.PROBE_FUNCS, name, lambda ctx, args: (None, {"status": "test_stub"}))
    for name in list(donecheck.BASELINES):
        monkeypatch.setitem(donecheck.BASELINES, name, lambda args: None)
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "game_in_front", lambda: "")
    C.clear_cache()
    yield
    C.clear_cache()


@pytest.fixture()
def demo(monkeypatch: pytest.MonkeyPatch) -> C.Loaded:
    monkeypatch.setattr(C, "CONNECTIONS_DIR", FIXTURES)
    C.clear_cache()
    return C.load()


# =====================================================================================================================
# 1. The packaged app loads all seven skills
# =====================================================================================================================


def test_the_shipped_folder_loads_all_seven_skills_with_no_error() -> None:
    loaded = C.load()
    assert set(loaded.keys) == SHIPPED_SKILLS and loaded.errors == ()


def test_pyinstaller_bundles_every_connection_and_site_file_where_the_code_looks() -> None:
    """The spec's own datas call (``collect_data_files("yuki", includes=["**/*.toml"])``) puts every connection file
    next to ``yuki/skills/connection.py`` and every site file next to ``yuki/connect/templates.py``: the paths the code
    builds from ``__file__`` (``connections/``, ``sites/``) exist in the bundle."""
    spec = (ROOT / "packaging" / "yuki.spec").read_text(encoding="utf-8")
    assert 'collect_data_files("yuki", includes=["**/*.toml"])' in spec
    assert 'hiddenimports = collect_submodules("yuki")' in spec
    hooks = pytest.importorskip("PyInstaller.utils.hooks")
    bundled = {(Path(src).resolve(), Path(dest).as_posix()) for src, dest in
               hooks.collect_data_files("yuki", includes=["**/*.toml"])}
    for folder, dest in ((C.CONNECTIONS_DIR, "yuki/skills/connections"),
                         (ROOT / "yuki" / "connect" / "sites", "yuki/connect/sites")):
        files = sorted(folder.glob("*.toml"))
        assert files
        for f in files:
            assert (f.resolve(), dest) in bundled, f.name
    assert C.CONNECTIONS_DIR == Path(C.__file__).with_name("connections")


def test_every_driver_the_data_names_resolves_through_the_import_system() -> None:
    """Drivers, probes and window-state readers are found with ``importlib.util.find_spec`` (never a file path), and
    each is a plain submodule of ``yuki.skills`` (so ``collect_submodules("yuki")`` puts it in the archive)."""
    import pkgutil

    import yuki.skills as pkg

    names: set[str] = set()
    for skill in C.load().skills:
        if skill.state_reader:
            names.add(skill.state_reader)
        for r in skill.routes:
            if r.driver:
                names.add(r.driver)
            for check in (r.done, r.recheck):
                call = (check.args or {}).get("call") if check is not None and check.probe == "driver" else None
                if call and "." in str(call):
                    names.add(str(call).split(".", 1)[0])
    assert {"spotify_route", "chat", "web_route"} <= names
    found = {m.name for m in pkgutil.iter_modules(pkg.__path__)}
    for name in names:
        assert C.driver_exists(name), name
        assert importlib.util.find_spec(f"yuki.skills.{name}") is not None
        assert name in found, name


# =====================================================================================================================
# 2. The doctor's skills line
# =====================================================================================================================


def test_the_doctor_says_what_is_loaded_proven_and_run(tmp_path: Path) -> None:
    from yuki.core import doctor

    db = tmp_path / "memory.db"
    s = Store.open(db)
    R.seed(s, C.load())
    now = time.time()
    for outcome, checked in (("done", True), ("done", True), ("unverified", None)):
        key = R.start_run(s, skill="volume", origin="voice", t0=now - 60)
        R.finish_run(s, key, outcome=outcome, checked=checked, t_done=now - 59)
    s.close()
    got = doctor.check_skills(db, settings=SimpleNamespace(skills=True), now=now)
    assert got.status == doctor.OK
    assert got.detail.startswith("7 loaded, 0 invalid connection files")
    assert "Spotify routes proven: covered no, other screen no, minimised no; mixer linked both ways no" in got.detail
    assert "3 runs in the last week: 2 done, 1 couldn't confirm, 0 false done" in got.detail
    assert "Meet tab learned: no" in got.detail
    s = Store.open(db)
    key = R.start_run(s, skill="meet.open", origin="voice", t0=now - 30)
    R.finish_run(s, key, outcome="done", checked=True, t_done=now - 29)
    R.set_recheck(s, key, False)
    s.set_nudge_state("site_tabs:Google Meet", json.dumps({"process": "zen.exe", "runtime_ids": ["x"]}))
    s.close()
    got = doctor.check_skills(db, settings=SimpleNamespace(skills=True), now=now)
    assert got.status == doctor.WARN and "1 false done" in got.detail and "Meet tab learned: yes" in got.detail
    assert doctor.check_skills(db, settings=SimpleNamespace(skills=False)).status == doctor.SKIP
    import inspect

    # in the checklist (read from the source: running every check would read the real database)
    assert "(SKILLS_CHECK, lambda: check_skills(settings=settings))" in inspect.getsource(doctor.run_checks)


# =====================================================================================================================
# 3. One request id: the thinker's run_skill and requests.csv
# =====================================================================================================================


def _reply(text: str) -> Any:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage,
                           model="claude-sonnet-4-5", id="msg")


def test_a_pinned_request_writes_its_skill_on_requests_csv_and_its_run_skill_gets_the_log_id(tmp_path: Path) -> None:
    import csv

    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings
    from yuki.log.events import SessionLogger

    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv", judge=False,
                        consult=False)
    logger = SessionLogger(settings.sessions_dir, console=None, session_id="20261010-2-worker", quiet=True)
    replies = [_reply("Hello there.") for _ in range(6)]
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: replies.pop(0)))
    agent = Agent(settings, logger, client=client, prewarm=False, record_requests=True, lane="worker",
                  memory=MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory"))))
    seen: list[Any] = []
    real = agent.logger.user_message

    def spy(text: str) -> None:
        real(text)
        seen.append(dict(agent.dispatcher.skill_context))

    agent.logger.user_message = spy   # type: ignore[method-assign]
    pin = {"key": "volume", "app": "audio", "goal": "set the volume", "slots": {}, "done_words": "x",
           "done": {"probe": "read_back"}, "tried": []}
    list(agent.run("turn it down", request_id="voice-20261010-1-t3", skill=pin))
    logger.close()
    # the context is read right after user_message began the id, and the request's own id is what run_skill gets
    assert agent.dispatcher.skill_context.get("request_id") == "voice-20261010-1-t3"
    with (tmp_path / "r.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["request_id"] == "voice-20261010-1-t3"
    assert rows[0]["skill"] == "volume" and rows[0]["route"] == "C"


# =====================================================================================================================
# 4. A voice skill hand: fWire's hand id on its lines and marker; the turn on the ledger; follow joins them
# =====================================================================================================================


class _Mem:
    def __init__(self, client: MemoryClient) -> None:
        self.client = client
        self.session_id = "s-test"
        self.marks: list[tuple[str, str, str | None]] = []

    def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
        return fn(self.client)

    def acting_begin(self, token: str, request: str, *, lane: str = "", request_id: str | None = None) -> bool:
        self.marks.append(("begin", token, request_id))
        return True

    def acting_end(self, token: str) -> bool:
        self.marks.append(("end", token, None))
        return True


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.lines.append((kind, fields))


class _Params:
    def __init__(self, name: str, args: dict) -> None:
        self.function_name = name
        self.arguments = args
        self.tool_call_id = "call-1"
        self.pushed: list[Any] = []
        self.results: list[tuple[dict, Any]] = []
        self.llm = SimpleNamespace(push_frame=self._push)

    async def _push(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


def test_a_voice_skill_hand_carries_its_hand_id_and_the_ledger_its_turn(tmp_path: Path, demo: C.Loaded,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import hands as H

    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(client.store, demo)
    mem, log = _Mem(client), _Log()
    desk = SimpleNamespace(game_front=lambda: "", get_desktop_overview=lambda: {"windows": [], "foreground_hwnd": None},
                           format_overview=lambda overview: "")
    hands = H.Hands(desk)
    hands.bridge = None
    hands.log = log
    hands._dispatcher = SimpleNamespace(overview_text=lambda: ("", {"foreground_hwnd": None}, 0.0))
    hands.skill_keys = ("demo.volume", "demo.home", "demo.play")
    hands.skill_memory = mem
    hands.memory = mem
    hands.skill_settings = SimpleNamespace(skills=True, skills_volume_step=10, skills_disable_after=2)
    hands.words = lambda: ["open demo home"]
    turns = iter(["20261010-1-4", "20261010-1-5"])
    hands.turn_key = lambda: next(turns)            # a second read would name another turn: read once per hand
    opened: list[Any] = []
    monkeypatch.setitem(E.VERB_FUNCS, "open_uri",
                        lambda call: opened.append(call) or E.RouteResult(ran=True, outcome="opened"))
    monkeypatch.setitem(donecheck.PROBE_FUNCS, "foreground", lambda ctx, args: (True, {"status": "stub"}))
    params = _Params("run_skill", {"say_first": "", "skill": "demo.home", "slots": {}})
    asyncio.run(hands.handle(params))
    assert opened, "the focus rung ran"
    hand_id = "voice-20261010-1-4-h1"
    begins = [m for m in mem.marks if m[0] == "begin"]
    assert begins and begins[0][2] == hand_id                       # the acting marker carries the hand id
    lines = [f for k, f in log.lines if k == "hands" and f.get("op") == "run_skill"]
    assert lines and all(f.get("request_id") == hand_id for f in lines)
    engine_lines = [f for k, f in log.lines if k in ("skill_run", "skill_resolve")]
    assert engine_lines and all(f.get("request_id") == "20261010-1-4" for f in engine_lines)
    assert all(f.get("hand_id") == hand_id for f in engine_lines)   # sWire r1 L2: which hand, beside the turn (D19)
    rows = client.store._query("SELECT request_id FROM skill_runs")
    assert [r[0] for r in rows] == ["20261010-1-4"]
    assert hands._hand_ids == {}
    client.close()


def test_follow_joins_a_voice_skill_hand_to_its_turns_lines(tmp_path: Path) -> None:
    from yuki.log import follow as F

    assert F.related_ids("voice-20261010-1-4-h2") == ("voice-20261010-1-4-h2", "20261010-1-4")
    assert F.related_ids("voice-20261010-1-4-t7") == ("voice-20261010-1-4-t7",)
    assert F.related_ids("20261010-1-4") == ("20261010-1-4",)
    voice = tmp_path / "logs" / "voice"
    voice.mkdir(parents=True)
    now = time.time()
    lines = [{"type": "hands", "ts": now, "op": "run_skill", "request_id": "voice-20261010-1-4-h2"},
             {"type": "skill_run", "ts": now + 0.1, "skill": "volume", "request_id": "20261010-1-4"},
             {"type": "hands", "ts": now + 0.2, "op": "scroll", "request_id": "voice-20261010-1-4-h3"},
             {"type": "skill_run", "ts": now + 0.3, "skill": "volume", "request_id": "20261010-1-5"}]
    (voice / "20261010-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    out = F.follow("voice-20261010-1-4-h2", logs=tmp_path / "logs")
    got = [(r["type"], r.get("request_id")) for r in out["records"] if r["_source"] == "voice"]
    assert got == [("hands", "voice-20261010-1-4-h2"), ("skill_run", "20261010-1-4")]


def test_follow_keeps_a_second_skill_hand_of_the_same_turn_apart(tmp_path: Path) -> None:
    """sWire review r1 L2: the talker called run_skill twice in one turn. Both hands' engine lines carry the turn
    (D19) and each its own ``hand_id``: following one hand gets its own engine lines only; following the turn gets
    both."""
    from yuki.log import follow as F
    from yuki.log.events import content_free

    voice = tmp_path / "logs" / "voice"
    voice.mkdir(parents=True)
    now = time.time()
    h1, h2, turn = "voice-20261010-1-4-h1", "voice-20261010-1-4-h2", "20261010-1-4"
    lines = [{"type": "hands", "ts": now, "op": "run_skill", "request_id": h1},
             {"type": "skill_run", "ts": now + 0.1, "skill": "volume", "request_id": turn, "hand_id": h1},
             {"type": "hands", "ts": now + 0.2, "op": "run_skill", "request_id": h2},
             {"type": "skill_run", "ts": now + 0.3, "skill": "spotify.playlist", "request_id": turn, "hand_id": h2}]
    (voice / "20261010-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    def voice_of(request_id: str) -> list[tuple[str, str | None, str | None]]:
        out = F.follow(request_id, logs=tmp_path / "logs")
        return [(r["type"], r.get("request_id"), r.get("hand_id")) for r in out["records"] if r["_source"] == "voice"]

    assert voice_of(h1) == [("hands", h1, None), ("skill_run", turn, h1)]
    assert voice_of(h2) == [("hands", h2, None), ("skill_run", turn, h2)]
    assert [x[2] for x in voice_of(turn)] == [h1, h2]
    assert content_free({"type": "skill_run", "hand_id": h1})["hand_id"] == h1      # an id the logs keep as it is


def test_the_engine_writes_the_hand_id_beside_the_turn() -> None:
    lines: list[tuple[str, dict]] = []
    ctx = E.RunContext(request_id="20261010-1-4", hand_id="voice-20261010-1-4-h2",
                       log=lambda event, **f: lines.append((event, f)))
    E._emit(ctx, "skill_run", skill="volume", result="done")
    assert lines == [("skill_run", {"skill": "volume", "result": "done", "request_id": "20261010-1-4",
                                    "hand_id": "voice-20261010-1-4-h2"})]
    lines.clear()
    E._emit(E.RunContext(request_id="worker-1", log=lambda event, **f: lines.append((event, f))), "skill_run")
    assert "hand_id" not in lines[0][1]


# =====================================================================================================================
# 5. The router knows the talker's skills
# =====================================================================================================================


def test_the_router_gets_a_skills_line_without_slots_or_names() -> None:
    from yuki.skills.menu import menu_text
    from yuki.voice.router import ROUTER_PROMPT, SKILLS_CHARS, router_request, skills_line

    menu = menu_text(None, audience="talker")
    line = skills_line(menu)
    keys = re.findall(r"^- ([^:]+):", line, flags=re.M)
    assert set(keys) == SHIPPED_SKILLS and "[" not in line and len(line) <= SKILLS_CHARS
    content = router_request("play my focus playlist", skills=menu)["messages"][0]["content"]
    assert content.startswith("[Skills line") and content.endswith('Their words now: "play my focus playlist"')
    assert "Skills line" not in router_request("hi")["messages"][0]["content"]
    assert "Skills line" in ROUTER_PROMPT


# =====================================================================================================================
# 6. The thinker's run_skill: the "Act" switch and a slow memory
# =====================================================================================================================


class _Policy:
    def __init__(self, off: set[str]) -> None:
        self.off = off

    def app_off(self, app: str | None) -> str | None:
        return "Demo Player" if app and str(app).casefold().removesuffix(".exe") in self.off else None


def _dispatcher() -> Any:
    from yuki.agent.tools import Dispatcher

    d = Dispatcher.__new__(Dispatcher)
    d.skill_context = {}
    d.act_policy = None
    return d


def test_the_thinkers_run_skill_never_presses_in_an_app_whose_act_switch_is_off(tmp_path: Path, demo: C.Loaded,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(client.store, demo)
    pressed: list[Any] = []
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke",
                        lambda call: pressed.append(call) or E.RouteResult(ran=True, outcome="pressed"))
    d = _dispatcher()
    d.act_policy = _Policy({"demoplayer"})
    d.skill_context = {"memory": client, "words": ["set demo player to 40"], "request_id": "r-1", "log": None,
                       "settings": SimpleNamespace(skills=True)}
    out = d._do_run_skill({"skill": "demo.volume", "slots": {"level": 40, "control": "in_app"}})
    assert not pressed and out.ok is False
    assert out.payload["refused"] == "act_off" and out.payload["handoff"] is None
    assert "You turned off clicking and typing in Demo Player" in out.content[0]["text"]
    rows = client.store._query("SELECT outcome FROM skill_runs")
    assert [r[0] for r in rows] == ["refused"]
    d.act_policy = _Policy(set())
    d._do_run_skill({"skill": "demo.volume", "slots": {"level": 40, "control": "in_app"}})
    assert pressed                                                      # with Act on, the same rung presses
    client.close()


def test_a_stand_in_desk_never_reads_the_users_real_act_switches(tmp_path: Path, demo: C.Loaded,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """sWire review r1 L1: with no policy of its own, the thinker's run_skill on a stand-in desk never reads the
    shared (real) policy file; on the real desk it does; and the agent loop gives the dispatcher its Authorizer's
    policy (a test's or eval's temp file) at each request."""
    from yuki.agent import judge
    from yuki.agent import tools as T
    from yuki.agent.loop import Agent

    reads: list[str] = []

    class _Real(_Policy):
        def app_off(self, app: str | None) -> str | None:
            reads.append(str(app))
            return super().app_off(app)

    from yuki.memory import gamegate

    monkeypatch.setattr(judge, "_shared_reader", lambda: _Real({"demoplayer"}))
    monkeypatch.setattr(gamegate, "game_in_front", lambda: "")         # no read of this PC's foreground window
    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    R.seed(client.store, demo)
    pressed: list[Any] = []
    monkeypatch.setitem(E.VERB_FUNCS, "uia_invoke",
                        lambda call: pressed.append(call) or E.RouteResult(ran=True, outcome="pressed"))
    d = _dispatcher()
    d.backend = SimpleNamespace()                                       # a stand-in desk
    d.skill_context = {"memory": client, "words": ["set demo player to 40"], "request_id": "r-1", "log": None,
                       "settings": SimpleNamespace(skills=True)}
    d._do_run_skill({"skill": "demo.volume", "slots": {"level": 40, "control": "in_app"}})
    assert pressed and reads == []
    pressed.clear()
    d.backend = T._LazyRealBackend.__new__(T._LazyRealBackend)          # the real desk (never started here)
    out = d._do_run_skill({"skill": "demo.volume", "slots": {"level": 40, "control": "in_app"}})
    assert not pressed and reads and out.payload["refused"] == "act_off"
    client.close()
    own = _Policy(set())
    agent = Agent.__new__(Agent)
    agent.authorizer = SimpleNamespace(policy=own)
    agent.dispatcher = SimpleNamespace(act_policy=None, skill_context={})
    agent.memory = SimpleNamespace(installed=False)
    agent.logger = SimpleNamespace(request_id="r-2", log=None)
    agent.settings = None
    agent._set_skill_context("set demo player to 40", ["set demo player to 40"])
    assert agent.dispatcher.act_policy is own and agent.dispatcher.skill_context["request_id"] == "r-2"


def test_a_resolve_memory_does_not_answer_runs_nothing_for_the_thinker(demo: C.Loaded,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []
    for name in list(E.VERB_FUNCS):
        monkeypatch.setitem(E.VERB_FUNCS, name, lambda call: ran.append(call) or E.RouteResult(ran=True))
    slow = SimpleNamespace(call=lambda what, fn, timeout_s=1.0: None)   # MemoryAccess's answer on a timeout
    got = E.run_for_thinker({"skill": "demo.volume", "slots": {"level": 40}}, ["set demo player to 40"], memory=slow)
    assert got.outcome == "refused" and got.say == E.LINE_MEMORY_SLOW and not ran
    # with no memory at all the files alone still resolve (as before)
    got = E.run_for_thinker({"skill": "demo.volume", "slots": {"level": 40}}, ["set demo player to 40"])
    assert got.outcome != "refused"


# =====================================================================================================================
# 7. The old learners see the skill; the daily taste seed
# =====================================================================================================================


def test_the_run_header_names_the_skill_and_the_old_learner_skips_it(tmp_path: Path) -> None:
    from yuki.memory import practice as P
    from yuki.memory.practice_learn import skill_of_run

    s = Store.open(tmp_path / "memory.db")
    got = P.record_run(s, {"source": "agent", "session_id": "s", "request_id": "worker-1", "lane": "worker",
                           "at": time.time(), "words": "invented", "outcome": "final", "steps": [],
                           "skill": "spotify.playlist"})
    assert got["ok"]
    header = P.run_header(s, got["run_key"])
    assert header is not None and header.signals.get("skill") == "spotify.playlist"
    assert skill_of_run(s, header) == "spotify.playlist"
    s.close()


def test_the_daily_taste_seed_runs_off_the_loop_and_logs_counts(tmp_path: Path) -> None:
    from yuki.memory import service

    s = Store.open(tmp_path / "memory.db")
    lines: list[tuple[str, dict]] = []
    thread = service._start_taste_seed(s, lambda kind, **f: lines.append((kind, f)))
    assert thread is not None
    thread.join(10)
    assert lines and lines[0][0] == "taste_seed" and lines[0][1].get("channels") == 0
    assert all(isinstance(v, (int, float)) for v in lines[0][1].values())
    s.close()
    src = (ROOT / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    assert "_start_taste_seed(store, log)" in src and "taste_seed_at = now + TASTE_SEED_EVERY_S" in src
