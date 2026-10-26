"""fWire: the wave F merge notes, wired (the design notes).

fLive's live sources on fMoments' hub in the memory service, the game gate's front source and refusal log in the tray,
the pattern call's game check, one ``request_id`` from the words to the result (the agent's marker, requests.csv, the
front desk's hand-off, the voice hands), D7 in the display proof, and the privacy file's new tables.

Offline: temp databases, stubs and invented names. Nothing reads the desktop; the memory service is read as source.
"""

from __future__ import annotations

import ast
import csv
import json
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory import moments as M
from yuki.memory.store import MEETING_GATE_CHECKPOINT, Store, TimelineRow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


# =====================================================================================================================
# 1. The memory service's wiring (read from the source: the service itself reads the live desktop)
# =====================================================================================================================


def test_the_service_wires_the_live_sources_onto_the_hub():
    src = (ROOT / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    ast.parse(src)
    for needle in ("CallEvidence(log=log, privacy=watcher.privacy, state=store)",
                   "SiteTabs(call_evidence, store=store, board=now_board, log=log)",
                   'getattr(call_evidence, "site_front_service", None)', "moments.tab_known_fn = site_tabs.known",
                   "watcher.on_screen = _on_screen", "_tabs.seen(screen)", "_hub.site(screen)",
                   "now_board.game_front_fn", "now_board.signals_fn", "calls.mic_holders()", "timeline.playing_apps()",
                   "VolumeWatch(", "hub_on_change(moments)", "SpotifyWatch(emit=hub_emit(moments)",
                   "moments.subscribe_media(spotify.on_media_change)", "moments.subscribe_front(spotify.on_front_change)",
                   "now_board.subscribe_layout(spotify.on_layout)", "spotify.beat()", "volume=volume_watch.stats()",
                   "spotify=spotify.stats()", "site_tabs=site_tabs.stats()", "volume_watch.stop()"):
        assert needle in src, needle
    # the three-way hook only once the hub exists; until then sightings go to the call evidence alone
    assert src.index("watcher.on_screen = call_evidence.seen") < src.index("moments.start()") \
        < src.index("watcher.on_screen = _on_screen")
    # the board's game_front and the live sources come after the game gate is built; the volume watcher stops first
    assert src.index("game_gate = GameGate(") < src.index("now_board.game_front_fn") < src.index("SpotifyWatch(")
    assert src.index("volume_watch.stop()") < src.index("moments.stop()")
    # review r1 L3: before the hub stops, the watcher's hook goes back to the call evidence and the Spotify reader stops
    stopping = src[src.index("volume_watch.stop()"):]
    assert (stopping.index("watcher.on_screen = call_evidence.seen") < stopping.index("spotify.stop()")
            < stopping.index("moments.stop()") < stopping.index("watcher.stop()"))
    # review r1 L4: the Spotify reader is built only with [moments] enabled (like the volume watcher)
    assert (src.index("live_on = moments_cfg is not None") < src.index("if live_on and moments_cfg.volume_events")
            < src.index("if live_on:") < src.index("SpotifyWatch(emit="))


def test_a_stopped_spotify_reader_starts_no_job_and_writes_no_moment(monkeypatch):
    """Review r1 L3: ``SpotifyWatch.stop()`` (the service calls it before the hub stops): a read already running
    writes no moment, and no later change, layout or probe timer starts a job."""
    from test_flive_spotify_watch import STORE_ID, make

    from yuki.connect import spotify_desktop as SD

    monkeypatch.setattr(SD, "process_identity", lambda pid: {"aumid": "", "family": "", "image": "Spotify.exe"})
    monkeypatch.setattr(SD, "is_spotify_pid", lambda pid: pid == 4242)
    watch, reader, _store, moments, _logs, clock = make()
    real_read = reader.read

    def read_then_stop(hwnd: int, budget_s: float, **kw: Any) -> Any:
        watch.stop()                     # the service stops while the Library read runs
        return real_read(hwnd, budget_s, **kw)

    reader.read = read_then_stop
    watch.on_media_change(started=[(STORE_ID, "Spotify", "x")], stopped=[])
    assert reader.reads and moments == []
    jobs = watch.counters["jobs"]
    watch.on_front_change("spotify", "arc")
    watch.on_front_change("arc", "spotify")
    watch.on_layout([{"pid": 4243, "hwnd": 9002, "share": 0.5}], foreground=1)
    watch.beat(now=clock.mono + 10 ** 6)
    assert watch.counters["jobs"] == jobs and moments == []


def test_the_board_carries_the_gates_game_and_says_unknown_for_a_stopped_gate():
    from yuki.memory.now import NowBoard, game_in_front

    board = NowBoard()
    board.game_front_fn = lambda: "valorant-win64-shipping"
    snap = board.snapshot()
    assert snap["game_front"] is True and snap["game_front_name"] == "valorant-win64-shipping"
    assert game_in_front(snap) is True
    board.game_front_fn = lambda: ""
    snap = board.snapshot()
    assert snap["game_front"] is False and snap["game_front_name"] is None
    board.game_front_fn = lambda: None                      # the gate's thread is gone: not known
    snap = board.snapshot()
    assert snap["game_front"] is None and snap["game_front_at"] is None and game_in_front(snap) is None


def test_the_trays_front_source_uses_a_fresh_board_and_asks_it_rarely():
    from yuki.memory.now import board_front_source

    now = [100.0]
    asked: list[float] = []
    board: dict[str, Any] = {"game_front": True, "game_front_at": time.time(), "game_front_name": "skyrunner"}

    class Client:
        def now(self, wait_s: float = 0.05) -> dict[str, Any] | None:
            asked.append(wait_s)
            return dict(board) if board else None

    source = board_front_source(lambda: Client(), clock=lambda: now[0])
    assert source() == "skyrunner" and asked == [0.05]
    assert source() == "skyrunner" and len(asked) == 1        # reused for FRONT_SOURCE_REUSE_S
    now[0] += 1.0
    board["game_front_at"] = time.time() - 5                 # older than 2 s: the local read stands
    assert source() is None
    now[0] += 1.0
    board.update(game_front_at=time.time(), game_front=False)
    assert source() is None
    now[0] += 1.0
    board.clear()                                            # no answer from the memory service
    assert source() is None
    now[0] += 1.0
    assert board_front_source(lambda: None, clock=lambda: now[0])() is None          # no memory key yet
    assert board_front_source(lambda: 1 / 0, clock=lambda: now[0])() is None         # never raises


def test_the_board_signals_come_from_the_call_worker_and_the_timeline():
    from yuki.memory.calls import CallWorker
    from yuki.memory.timeline import TimelineRecorder

    worker = CallWorker.__new__(CallWorker)
    worker._held = {"zen", "discord"}
    assert worker.mic_holders() == ["discord", "zen"]
    rec = TimelineRecorder.__new__(TimelineRecorder)
    rec._playing = [("spotifyab.spotifymusic_x!spotify", "Spotify", "A Title"), ("chrome", "Chrome", "Other"),
                    ("Unresolved.exe", "", "")]
    rec._app_ids = {"spotifyab.spotifymusic_x!spotify": "spotify"}
    assert rec.playing_apps() == ["chrome", "spotify", "unresolved"]       # app keys only, never a title


def test_site_front_notes_whether_the_meet_tab_was_identified(store):
    logged: list[tuple[str, dict]] = []
    h = M.MomentHub(store, log=lambda t, **f: logged.append((t, f)), config=lambda: M.MomentsConfig(),
                    calling_service=lambda host: "Google Meet", acting_fn=lambda: None, runs_fn=lambda lo, hi: [],
                    is_game=lambda p: False, page_entity=None)
    h.tab_known_fn = lambda hwnd: hwnd == 7
    h.site({"process": "zen", "hwnd": 7, "host": "meet.google.com", "blocked": "blocked_host", "at": 10.0})
    h.site({"process": "zen", "hwnd": 8, "host": "meet.google.com", "blocked": "blocked_host", "at": 11.0})
    h.tab_known_fn = lambda hwnd: 1 / 0
    h.site({"process": "zen", "hwnd": 9, "host": "meet.google.com", "blocked": "blocked_host", "at": 12.0})
    h.beat(13.0)
    rows = sorted(store.moments_between(kinds=["site_front"]), key=lambda r: r["at"])
    assert [r["facts"].get("tab_known") for r in rows] == [True, False, None]
    assert [f["tab_known"] for t, f in logged if t == "site_front"] == [True, False, None]


def test_moments_rule_version_rebuilds_the_page_moments_with_the_templates():
    assert M.RULE_VERSION == 3   # lCore (10 Oct): host-scope page moments and the entity key
    from yuki.connect import templates

    assert M.default_page_entity() is templates.page_entity
    assert templates.page_entity("www.youtube.com", "/watch?v=abc123") == ("youtube.watch", "abc123")


def test_loopback_keeps_no_unused_ctypes_import():
    src = (ROOT / "yuki" / "memory" / "loopback.py").read_text(encoding="utf-8")
    assert "c_float" not in src


# =====================================================================================================================
# 2. The privacy file's tables
# =====================================================================================================================


def test_the_privacy_file_lists_the_new_tables_with_the_code_defaults():
    from yuki.connect.spotify_desktop import SpotifySettings
    from yuki.memory import nudges

    text = (ROOT / "yuki" / "memory" / "privacy_default.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert M.MomentsConfig.from_dict(data["moments"]) == M.MomentsConfig()
    assert set(data["moments"]) == {"enabled", "volume_events", "media_titles", "refresh_s", "page_dwell_s",
                                    "keep_days", "host_dwell_s"}   # host_dwell_s: zNotes (10 Oct)
    spotify, default = SpotifySettings.from_dict(data["spotify"]), SpotifySettings()
    assert set(data["spotify"]) == {"enabled", "library_reads", "read_budget_s", "warm_budget_s", "state_probe"}
    assert all(getattr(spotify, k) == getattr(default, k) for k in data["spotify"])
    assert text.count("return_hold_s =") == 1 and data["nudges"]["return_hold_s"] == nudges.RETURN_HOLD_S
    assert data["trace"]["game_gate"] == "front"


# =====================================================================================================================
# 3. The game gate in the tray, the pattern call, hover and the voice hands' report
# =====================================================================================================================


def test_the_tray_logs_every_refusal_and_adds_the_boards_game(monkeypatch):
    from yuki.memory import gamegate
    from yuki.ui.app import YukiUi

    events: list[tuple[str, dict]] = []
    ui = SimpleNamespace(ui_log=SimpleNamespace(event=lambda name, **f: events.append((name, f))))
    YukiUi._wire_game_backstop(ui)
    assert callable(gamegate._front_source) and gamegate._refusal_listeners == [ui._refusal_listener]
    gamegate._refused("keys", "valorant-win64-shipping", "front")
    assert events == [("input_refused_game", {"action": "keys", "game": "valorant-win64-shipping", "where": "front"})]
    YukiUi._unwire_game_backstop(ui)
    assert gamegate._front_source is None and gamegate._refusal_listeners == []


def test_a_pattern_call_on_a_games_window_is_refused_and_never_falls_back(monkeypatch):
    from yuki.actions import gate, patterns
    from yuki.memory import gamegate

    seen: list[dict] = []

    def refuse(action: str, **target: Any) -> str | None:
        seen.append({"action": action, **target})
        # refused as the gate refuses a game: its refusal listeners fire (fHands, 18 Oct: the hands' gate tags a
        # refusal "game" only then; a stub's bare words read "blocked")
        return gamegate._refused(action, "skyrunner", "window")

    monkeypatch.setattr(gamegate, "_refusal_listeners", [gate._heard])
    monkeypatch.setattr(gamegate, "refuse_input", refuse)
    monkeypatch.setattr(patterns, "usable", lambda element, verb: "invoke")
    monkeypatch.setattr(patterns, "_root", lambda hwnd: pytest.fail("read the window after a refusal"))
    element = {"role": "Button", "name": "Play", "center": (10, 10), "patterns": ["invoke"]}
    result = patterns.element_action(0x1234, element, "click")
    assert result is not None and result.ok is False and result.details["refused"] == "game"
    assert seen == [{"action": "uia_pattern", "hwnd": 0x1234, "front_ok": True}]
    monkeypatch.setattr(gamegate, "refuse_input", lambda action, **t: None)
    monkeypatch.setattr(patterns, "_root", lambda hwnd: (_ for _ in ()).throw(RuntimeError("past the check")))
    with pytest.raises(RuntimeError, match="past the check"):
        patterns.element_action(0x1234, element, "click")   # allowed: it goes on to find the element


def test_hover_with_a_game_in_front_moves_nothing_and_says_so(monkeypatch):
    import yuki.actions.hover as hover_mod
    import yuki.actions.input as inp
    from yuki.actions import gate

    def refused_move(x: int, y: int, *, why: list[str] | None = None) -> bool:
        if why is not None:   # the gate's refusal, as _move passes it on: a game refused it (fHands, 18 Oct)
            why.append(gate.Refusal("a game (skyrunner) is in front: Yuki sends nothing while a game is in front",
                                    gate.GAME))
        return False

    monkeypatch.setattr(inp, "_move", refused_move)
    monkeypatch.setattr(inp, "_in_screen_bounds", lambda x, y: True)
    monkeypatch.setattr(inp, "_foreground_mismatch", lambda hwnd: None)
    monkeypatch.setattr(hover_mod, "_near", lambda automation, x, y, deadline: {})
    result = hover_mod.hover(10, 10)
    assert result.ok is False and result.details["refused"] == "game" and "skyrunner" in result.summary


def test_a_backstop_refusal_is_never_handed_to_the_thinker():
    from yuki.voice.hands import Hands

    facts: dict[str, Any] = {}
    refused = SimpleNamespace(ok=False, summary="a game (skyrunner) is in front", elapsed_ms=3.0,
                              details={"refused": "game"})
    ok, report, facts = Hands._report(refused, "Window: ", facts)
    assert ok is False and facts["refused"] == "game" and report.startswith("Window: a game")
    _ok, _r, plain = Hands._report(SimpleNamespace(ok=False, summary="x", details={}), "", {})
    assert "refused" not in plain


def test_the_media_key_schema_says_which_session_it_aims_at():
    from yuki.voice.hands import HAND_TOOLS

    text = next(t for t in HAND_TOOLS if t["name"] == "media_key")["description"]
    assert "playing, else the current one" in text and "desktop block marks" not in text


# =====================================================================================================================
# 4. One request id (fPrivacy R1.3)
# =====================================================================================================================


class _Block(SimpleNamespace):
    pass


def _reply(text: str) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[_Block(type="text", text=text)], stop_reason="end_turn", usage=usage, id="m",
                           model="m", role="assistant", model_dump=lambda **_: {})


class _Client:
    def __init__(self, n: int) -> None:
        self.replies = [_reply("Hello there.") for _ in range(n)]
        self.messages = SimpleNamespace(create=lambda **_: self.replies.pop(0))


def test_a_handed_off_request_keeps_its_id_in_the_log_and_requests_csv(tmp_path):
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings
    from yuki.log.events import SessionLogger

    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv", judge=False,
                        consult=False)
    logger = SessionLogger(settings.sessions_dir, console=None, session_id="20261010-1-worker", quiet=True)
    agent = Agent(settings, logger, client=_Client(2), prewarm=False, record_requests=True, lane="worker",
                  memory=MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory"))))
    list(agent.run("hello", request_id="20261010-1-frontdesk-3"))
    list(agent.run("hello again"))
    logger.close()
    records = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    first = [r for r in records if r.get("type") in ("user_message", "request_summary")][:2]
    assert {r.get("request_id") for r in first} == {"20261010-1-frontdesk-3"}
    with (tmp_path / "r.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["request_id"] for r in rows] == ["20261010-1-frontdesk-3", "20261010-1-worker-2"]


class _LaneAgent:
    tool_names = None
    last_summary = None
    awaiting_answer = False
    cancelled = False

    def __init__(self) -> None:
        self.runs: list[dict] = []

    def run(self, request: str, **kw: Any) -> Any:
        self.runs.append(kw)
        return iter(())

    def answer(self, text: str) -> None:
        pass

    def cancel(self, reason: str = "cancelled") -> None:
        pass

    def take_steers(self) -> list:
        return []


def test_the_front_desks_hand_off_carries_one_id_to_the_worker(tmp_path):
    import test_voice_v39  # noqa: F401 - the Qt application

    from yuki.config import Settings
    from yuki.ui.runtime import Job
    from yuki.ui.runtime import AgentRuntime
    from yuki.ui.uilog import UiLog

    events: list[tuple[str, dict]] = []
    log = UiLog(sessions_dir=tmp_path / "s", session_id=None)
    real_event = log.event
    log.event = lambda name, **f: (events.append((name, f)), real_event(name, **f))   # type: ignore[method-assign]
    worker, desk = _LaneAgent(), _LaneAgent()
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), log, worker_agent=worker,
                      front_desk_agent=desk, memory=SimpleNamespace(session_id="t"))
    rt._log_ids[5] = "20261010-1-frontdesk-2"          # what the front desk's lane kept when its run ended
    rt._on_lane_done("front_desk", 5, "close it", True, ["click"])
    job = rt.worker._jobs.get_nowait()
    assert job.log_request_id == "20261010-1-frontdesk-2" and 5 not in rt._log_ids
    handoff = next(f for name, f in events if name == "handoff")
    assert handoff == {"id": 5, "worker_id": job.id, "refused": ["click"], "request_id": "20261010-1-frontdesk-2"}
    rt.worker._drive(job)
    assert worker.runs[-1].get("request_id") == "20261010-1-frontdesk-2"
    rt.worker._drive(Job(99, "plain"))
    assert "request_id" not in worker.runs[-1]


def test_the_front_desks_lane_keeps_the_id_its_run_ended_with_for_the_hand_off(tmp_path):
    """Review r1 L1: the producer. The front desk's lane thread reads its logger's ``last_request_id`` after the run
    and keeps it before ``lane_done``; the worker's job then carries it (and nothing is left behind)."""
    import test_voice_v39  # noqa: F401 - the Qt application

    from yuki.config import Settings
    from yuki.log.events import ToolCall
    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    class Desk(_LaneAgent):
        tool_names = ("read_screen",)   # no hands: a click is refused, so the request goes to the worker

        def __init__(self) -> None:
            super().__init__()
            self.logger = SimpleNamespace(last_request_id=None)

        def run(self, request: str, **kw: Any) -> Any:
            self.runs.append(kw)
            self.logger.last_request_id = "20261010-091512-frontdesk-3"   # the logger ends the request in the run
            return iter([ToolCall(name="click", input={})])

    events: list[tuple[str, dict]] = []
    log = UiLog(sessions_dir=tmp_path / "s", session_id=None)
    real_event = log.event
    log.event = lambda name, **f: (events.append((name, f)), real_event(name, **f))   # type: ignore[method-assign]
    desk = Desk()
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), log,
                      worker_agent=_LaneAgent(), front_desk_agent=desk, memory=SimpleNamespace(session_id="t"))
    rt.front_desk.enqueue(Job(7, "close it"))
    rt.front_desk._jobs.put(None)          # the lane's loop ends after the one job
    rt.front_desk.run()                    # the lane thread's body, on this thread (lane_done connects directly)
    job = rt.worker._jobs.get_nowait()
    assert job.log_request_id == "20261010-091512-frontdesk-3" and rt._log_ids == {}
    assert next(f for name, f in events if name == "handoff")["request_id"] == "20261010-091512-frontdesk-3"


def test_follow_puts_a_front_desk_hand_off_together_across_the_runs_files(tmp_path, monkeypatch):
    """Review r1 M1: after the hand-off the worker writes the front desk's id in its own file and the UI log has the
    ``handoff`` / ``queued`` lines; ``follow`` reads the run's files (another run's file is never opened), and shows
    no word of either."""
    from yuki.log import follow as follow_mod
    from yuki.log.events import SessionLogger

    sessions = tmp_path / "logs" / "sessions"
    run = "20261010-091512"
    ui = SessionLogger(sessions, session_id=run, quiet=True)                       # the tray's UI log keeps words
    desk = SessionLogger(sessions, session_id=f"{run}-frontdesk", quiet=True, content=False)
    worker = SessionLogger(sessions, session_id=f"{run}-worker", quiet=True, content=False)
    other = SessionLogger(sessions, session_id="20261010-101010-worker", quiet=True, content=False)
    desk.user_message("invented private words")
    desk.log("tool_call", tool="click")
    desk.request_summary({"started_at": "2026-10-10T09:15:12"})
    rid = desk.last_request_id
    assert rid == f"{run}-frontdesk-1"
    ui.log("ui", event="queued", id=6, origin=5, waiting=0, request="invented private words", request_id=rid)
    ui.log("ui", event="handoff", id=5, worker_id=6, refused=["click"], request_id=rid)
    worker.begin_request(rid)
    worker.user_message("invented private words")
    worker.log("tool_call", tool="click")
    worker.log("tool_result", tool="click", outcome="ok")
    worker.request_summary({"started_at": "2026-10-10T09:15:20"})
    other.user_message("another run")
    for lg in (ui, desk, worker, other):
        lg.close()
    opened: list[str] = []
    real = follow_mod._jsonl

    def spy(path: Path, needle: str | None = None) -> Any:
        opened.append(path.name)
        return real(path, needle)

    monkeypatch.setattr(follow_mod, "_jsonl", spy)
    out = follow_mod.follow(rid, logs=tmp_path / "logs")
    by_file: dict[str, list[str]] = {}
    for r in out["records"]:
        if r["_source"] == "session":
            by_file.setdefault(r["_file"], []).append(r.get("event") or r["type"])
    assert sorted(by_file[f"{run}-worker.jsonl"]) == ["request_summary", "tool_call", "tool_result", "user_message"]
    assert sorted(by_file[f"{run}-frontdesk.jsonl"]) == ["request_summary", "tool_call", "user_message"]
    assert sorted(by_file[f"{run}.jsonl"]) == ["handoff", "queued"]
    handoff = next(r for r in out["records"] if r.get("event") == "handoff")
    assert handoff["id"] == 5 and handoff["worker_id"] == 6
    assert "20261010-101010-worker.jsonl" not in opened
    assert "invented" not in json.dumps(out, ensure_ascii=False)
    assert {p.name for p in follow_mod._session_files(sessions, f"{run}-frontdesk-1")} == {
        f"{run}.jsonl", f"{run}-frontdesk.jsonl", f"{run}-worker.jsonl"}
    # the run of a time-shaped id is its time; of a custom (test) id, the id without its lane
    assert follow_mod._run_of(f"{run}-frontdesk") == run and follow_mod._run_of(run) == run
    assert follow_mod._run_of("s1-frontdesk") == "s1"


def test_a_spoken_request_handed_to_the_thinker_carries_its_turns_id(tmp_path):
    import asyncio

    import test_voice_v39  # noqa: F401 - the Qt application
    from test_voice_v39 import FakeRuntime

    class IdRuntime(FakeRuntime):
        def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text",
                   log_request_id: str | None = None, **kw: Any) -> tuple[str, int]:
            if log_request_id is not None:
                kw["log_request_id"] = log_request_id
            return super().submit(text, origin_hwnd=origin_hwnd, source=source, **kw)

    async def main() -> tuple[Any, Any, Any]:
        from yuki.voice.bridge import VoiceBridge

        rt, old = IdRuntime(), FakeRuntime()
        bridge, bridge2 = VoiceBridge(rt, foreground=lambda: 42), VoiceBridge(old, foreground=lambda: 42)
        for b in (bridge, bridge2):
            b.attach(asyncio.get_running_loop())
            b.turn_key = lambda: "20261010-1-4"
        job = bridge._new_job("pause the music")
        bridge._submit(job, "pause the music", None)
        bridge2._submit(bridge2._new_job("pause"), "pause", None)
        return rt, job, old

    rt, job, old = asyncio.run(main())
    assert job.turn_id == "20261010-1-4"
    assert rt.submitted[0][2]["log_request_id"] == f"voice-20261010-1-4-t{job.token}"
    assert "log_request_id" not in old.submitted[0][2]                  # a runtime without it is never sent it

    from yuki.config import Settings
    from yuki.ui.runtime import AgentRuntime
    from yuki.ui.uilog import UiLog

    runtime = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                           UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=_LaneAgent(),
                           front_desk_agent=_LaneAgent(), memory=SimpleNamespace(session_id="t"))
    runtime.submit("pause the music", source="voice", log_request_id="voice-20261010-1-4-t1")
    assert runtime.worker._jobs.get_nowait().log_request_id == "voice-20261010-1-4-t1"


def test_a_voice_hand_marks_its_acting_and_its_trace_with_one_id(tmp_path):
    from yuki.memory import acting
    from yuki.voice.hands import Hands

    db = tmp_path / "memory.db"
    began: list[dict] = []

    class Memory:
        def acting_begin(self, token: str, request: str, *, lane: str = "", request_id: str | None = None) -> bool:
            began.append({"token": token, "lane": lane, "request_id": request_id})
            return acting.begin(db, token, request, lane=lane, request_id=request_id)

        def acting_end(self, token: str) -> bool:
            return acting.end(db, token)

    marked: list[Any] = []
    hands = SimpleNamespace(memory=Memory(), words=lambda: ["pause it"], shown=lambda: [], turn_key=lambda: "S-4",
                            _trace_n=0, _hand_ids={})
    hands._call_safely = lambda fn, default: Hands._call_safely(hands, fn, default)
    hands._mint_hand_id = lambda call=None: Hands._mint_hand_id(hands, call)
    # qVoice: _acting_run is the _acting marker around _run(..., bracket=)
    hands._acting = lambda n, h=None: Hands._acting(hands, n, h)
    hands._run = lambda name, args, **k: (marked.append(acting.current(db)), (True, "ok", {}))[1]
    hand_id = Hands._mint_hand_id(hands, "call-1")
    assert hand_id == "voice-S-4-h1"
    assert Hands._acting_run(hands, "window", {}, hand_id) == (True, "ok", {})
    assert began[-1]["request_id"] == "voice-S-4-h1" and marked[-1].request_id == "voice-S-4-h1"
    assert Hands._trace_ids(hands, "call-1")[0] == "voice-S-4-h1"      # the trace reuses the hand's id
    assert Hands._trace_ids(hands, "call-2")[0] == "voice-S-4-h2"      # a trace with no minted id gets its own
    assert acting.current(db) is None                                    # the marker withdrawn after the hand

    class OldMemory(Memory):    # a stand-in without request_id: the marker still goes up, with the token's id
        def acting_begin(self, token: str, request: str, *, lane: str = "") -> bool:   # type: ignore[override]
            began.append({"token": token})
            return True

    hands.memory = OldMemory()
    assert Hands._acting_run(hands, "window", {}, "voice-S-4-h3")[0] is True and "request_id" not in began[-1]


def test_a_voice_hands_own_log_line_carries_its_id():
    """Review r1 L2: the voice log's ``hands`` line (ok, ms, verified) names the hand's id, the one on its acting
    marker, so ``follow voice-...-h<n>`` finds the hand's outcome; a fact of the same name never replaces it."""
    import asyncio

    from test_voice_v39 import _bridge, _hand_params
    from test_voice_v312 import _ScrollDesk, _scroll_hands

    logged: list[tuple[str, dict]] = []
    marked: list[Any] = []

    async def main() -> Any:
        _rt, bridge = _bridge()
        hands = _scroll_hands(_ScrollDesk(), bridge)
        hands.turn_key = lambda: "20261010-1-4"
        hands.log = SimpleNamespace(event=lambda t, **f: logged.append((t, f)))
        real_run = hands._acting_run
        hands._acting_run = lambda name, args, hand_id=None: (marked.append(hand_id), real_run(name, args, hand_id))[1]
        hands._act = lambda name, args, facts: (True, "scrolled Document down: 10% -> 20%",
                                                 {**facts, "verified": True, "request_id": 7})
        await hands.handle(_hand_params("scroll", {"window": 7, "direction": "down", "say_when_done": ""}))
        return hands

    hands = asyncio.run(main())
    line = next(f for t, f in logged if t == "hands" and "ok" in f)
    assert line["request_id"] == marked[-1] and line["request_id"].startswith("voice-20261010-1-4-h")
    assert line["ok"] is True and line["verified"] is True and line["call"] == "h-scroll"
    assert hands._hand_ids == {}                                         # popped after the line, as before


# =====================================================================================================================
# 5. D7: the display proof and the moments agree
# =====================================================================================================================


def _row(store: Store, start: float, end: float, process: str, app: str, meeting: str | None, mic_s: float = 0.0,
         host: str | None = None) -> None:
    store.save_timeline(TimelineRow(None, start, end, "present", process, app, host=host, active_s=end - start,
                                    meeting=meeting, mic_s=mic_s))


def _call(store: Store, process: str, app: str, via: str, at: float, end: float) -> None:
    store.update_call(store.start_call(app=app, process=process, via=via, at=at), ended_at=end)


def test_a_microphone_hold_of_an_app_with_a_seen_call_reads_as_a_meeting_as_the_moments_count_it(store):
    t0 = time.time() - 20 * 86400
    _call(store, "zen", "Zen", "screen", t0 - 86400, t0 - 86000)          # a seen call of Zen on record
    _call(store, "fl64", "FL Studio", "mic", t0 - 50000, t0 - 49000)        # the first call row is before every row
    _row(store, t0, t0 + 60, "zen", "Zen", "Zen", mic_s=40.0)               # a hold, no call row around it
    _row(store, t0 + 3600, t0 + 3660, "zen", "Zen", "Zen", mic_s=0.0)       # no microphone: not a hold
    _row(store, t0 + 7200, t0 + 7400, "fl64", "FL Studio", "FL Studio", mic_s=120.0)   # never seen in a call
    _row(store, t0 + 9000, t0 + 9200, "valorant-win64-shipping", "VALORANT", "VALORANT", mic_s=90.0)   # a game
    store.set_source_checkpoint(MEETING_GATE_CHECKPOINT, str(time.time() + 60))   # rows from before the live gate
    store._meeting_gate_since = None
    lo, hi = t0 - 1, t0 + 10000
    for rows in (store.timeline_between(lo, hi), store.timeline_states(lo, hi),
                 [r for p in ("zen", "fl64", "valorant-win64-shipping") for r in store.timeline_of(p, lo)]):
        got = {(r.process, int(r.started_at - t0)): r.meeting for r in rows if r.process}
        assert got == {("zen", 0): "Zen", ("zen", 3600): None, ("fl64", 7200): None,
                       ("valorant-win64-shipping", 9000): None}
    ex = M.Extractor(store, config=lambda: M.MomentsConfig(keep_days=90), is_game=M.game_judge(store),
                     page_entity=None, runs_fn=lambda lo, hi: [])
    ex.rebuild(now=time.time())
    joins = [(r["app"], int(r["at"] - t0), r["evidence"]) for r in store.moments_between(kinds=["call_join"])
             if r["at"] >= t0 - 1]
    assert joins == [("zen", 0, "hold")]          # the one row the display keeps without a call row is the one hold
