"""fx2 (the design notes): the memory APIs the voice needs (29-build §3 items 1-3) and
the multi-part opening ("Open Chrome and restore the pages.": the talker opened Chrome and the rest was never done).

* ``record_hand_trace(injected=, cited=, control=)``: the playbooks shown and followed reach the ledger from a hand
  itself; the control's role and name are kept with the step.
* ``practice(detail=True)``: each item with its structured body (steps, executable steps, slots, what, instead).
* Executable steps: a distilled procedure keeps, beside its text, the steps the talker's skill runner runs as they are
  (a control by role and name, keys, an app, an expected state grounded in the trace; never a window id, never typed
  text); the voice shows them as run_skill takes them and runs the way's own steps.
* ``then`` on the opening hands: an opening that leaves part of the request gives the talker its turn back with the
  screen once it settled, instead of ending the turn.

Offline: temp databases, a word-bag embedder, stand-in desks and models; no device, desktop or real memory.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_c_practice import NOW, Worker, Words, step, vec  # noqa: F401
from test_voice_v316 import _no_real_audio_devices  # noqa: F401 - rule 7: every sounddevice stream fails the test
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


# -- 1. record_hand_trace(injected=, cited=, control=) --------------------------------------------------------------


def test_a_hand_trace_takes_the_playbooks_shown_and_cited_and_its_control(store):
    pit = P.add_playbook(store, {"kind": "pitfall", "intent": "lower a volume", "apps": ["chrome.exe"],
                                 "body": {"text": "media keys move everything", "instead": "app_volume"}},
                         events=[{"type": "observed", "run_key": "r1", "at": NOW}], vector=vec("lower a volume"),
                         now=NOW)
    way = P.add_playbook(store, {"kind": "procedure", "intent": "restore the pages", "apps": ["chrome.exe"],
                                 "body": {"steps": ["Click Restore"]}},
                         events=[{"type": "observed", "run_key": "r2", "at": NOW}], vector=vec("restore pages"),
                         now=NOW)
    got = P.record_hand_trace(store, session_id="S1", name="click", args={"window": 4242, "item": 3, "named": "restore"},
                              ok=True, verified=True, report="clicked", ms=80.0, at=NOW, app="chrome.exe",
                              target='Button "Restore" in "Restore pages?"', words="restore the pages",
                              injected=[pit, f"pb:{way}"], cited=[way], control="Button|Restore")
    assert got["ok"]
    head = P.run_header(store, got["run_key"])
    assert head.injected == [pit, way] and head.cited == [way]
    types = {pid: [e.type for e in P.ledger(store, pid)] for pid in (pit, way)}
    assert "injected" in types[pit] and "cited" not in types[pit] and {"injected", "cited"} <= set(types[way])
    s = P.run_steps(store, got["run_key"])[0]
    assert s.cited == [way]
    inp = json.loads(s.input)
    assert inp["role"] == "Button" and inp["name"] == "Restore" and "window" not in inp and "item" not in inp
    assert "4242" not in s.input                              # a window number is never meaning
    assert 4242 in head.signals["numbers"]
    # the control's identity is the agent loop's (the same app, tool and "role|name")
    rows = store._query("SELECT playbooks FROM runs WHERE run_key=? AND step=1", (got["run_key"],))
    assert json.loads(rows[0]["playbooks"])["control"] == P.control_key(store, "chrome.exe", "click", "Button|Restore")


def test_the_client_and_memory_access_pass_injected_cited_and_control_through(tmp_path):
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    client = MemoryClient(Store.open(tmp_path / "memory.db"))
    pid = P.add_playbook(client.store, {"kind": "pitfall", "intent": "lower a volume", "apps": ["chrome.exe"],
                                        "body": {"text": "x", "instead": "y"}},
                         events=[{"type": "observed", "run_key": "r1", "at": NOW}], vector=vec("lower a volume"))
    access = MemoryAccess(open_client=lambda: client, session_id="voice-1")
    assert access.record_hand_trace(name="app_volume", args={"app": "Yuki.exe", "change": -20}, ok=True,
                                    verified=True, report="80 -> 60", ms=90.0, app="Yuki.exe",
                                    words="lower your volume", injected=[pid], control="Slider|Volume")
    assert access.flush_turns(5)
    head = P.pending_runs(client.store)[0]
    assert head.injected == [pid] and head.session_id == "voice-1"
    # the voice's probe sees the fields (it then passes them with the hand instead of a record_run of its own)
    from yuki.voice.hands import _takes_kw

    assert _takes_kw(access.record_hand_trace, "injected") and _takes_kw(access.record_hand_trace, "control")
    client.close()


# -- 2. practice(detail=True) -------------------------------------------------------------------------------------


def _restore_way(store: Store, run: list[dict[str, Any]] | None = None) -> int:
    body = {"steps": ["Open Chrome", 'Click the Button "Restore"'], "slots": []}
    if run is not None:
        body["run"] = run
    return P.add_playbook(store, {"kind": "procedure", "intent": "restore the browser pages after a crash",
                                  "apps": ["chrome.exe"], "body": body},
                          events=[{"type": "observed", "run_key": "r1", "at": NOW - 86400},
                                  {"type": "observed", "run_key": "r2", "at": NOW - 2 * 86400},
                                  # appLearn: a way from a run is offered once that run was a verified success
                                  {"type": "succeeded", "run_key": "r1", "at": NOW - 86400}],
                          vector=vec("restore the browser pages after a crash"), now=NOW)


RUN = [{"do": "open_app", "name": "Google Chrome"},
       {"do": "click", "role": "Button", "name": "Restore", "expect": "Restore"}]


def test_practice_detail_gives_each_item_its_structured_body(store):
    from yuki.memory.api import MemoryClient

    way = _restore_way(store, RUN)
    pit = P.add_playbook(store, {"kind": "pitfall", "intent": "restore pages", "apps": ["chrome.exe"],
                                 "body": {"text": "reopened tabs one by one", "instead": "click Restore in the bubble"}},
                         events=[{"type": "observed", "run_key": "r3", "at": NOW}], vector=vec("restore pages"),
                         now=NOW)
    client = MemoryClient(store)
    client._embed = lambda text, wait=None: Words().embed([text])[0]
    plain = client.practice("restore the browser pages", open_apps=["chrome.exe"], now=NOW)
    assert plain["ways"] and "run" not in plain["ways"][0] and "what" not in plain["pitfalls"][0]
    got = client.practice("restore the browser pages", open_apps=["chrome.exe"], now=NOW, detail=True)
    w = next(i for i in got["ways"] if i["id"] == way)
    assert w["run"] == RUN and w["steps"][1].startswith("Click") and w["slots"] == []
    p = next(i for i in got["pitfalls"] if i["id"] == pit)
    assert p["what"] == "reopened tabs one by one" and p["instead"] == "click Restore in the bubble"
    assert "Instead:" in p["text"]                         # the rendered text is still there for older callers


# -- 3. executable steps --------------------------------------------------------------------------------------------


def test_executable_steps_name_controls_keys_and_apps_never_ids_or_typed_text():
    ok = dict(kind="action", ok=True, verdict=True)
    assert P.executable_step(dict(ok, tool="click", input={"role": "Button", "name": "Restore", "element": 7})) == {
        "do": "click", "role": "Button", "name": "Restore"}
    assert P.executable_step(dict(ok, tool="click", input={"element": 7, "read": 2})) is None   # no control name
    assert P.executable_step(dict(ok, tool="launch_app", input={"query": "Google Chrome"})) == {
        "do": "open_app", "name": "Google Chrome"}
    assert P.executable_step(dict(ok, tool="hotkey", input={"keys": ["Ctrl", "K"]})) == {"do": "keys",
                                                                                         "keys": ["ctrl", "k"]}
    assert P.executable_step(dict(ok, tool="press", input={"key": "enter", "times": 3})) is None
    typed = P.executable_step(dict(ok, tool="type_text", input={"text": "secret words"}), slot="channel")
    assert typed == {"do": "type", "slot": "channel"} and "secret" not in json.dumps(typed)
    assert P.executable_step(dict(ok, tool="open_url", input={"url": "https://example.com/q=abc"}),
                             literal_ok=False) is None   # an address in a way with slots may carry what changes
    assert P.executable_step(dict(ok, tool="scroll", input={"dy": -5})) is None   # pixels are no skill step
    assert P.executable_step(dict(ok, tool="window_action", input={"action": "maximize"})) is None
    assert P.executable_step(dict(ok, tool="click", verdict=False, input={"name": "Restore"})) is None
    # a skill run's own step, kept as written
    assert P.executable_step(dict(ok, tool="launch_app", input={"do": "open_app", "name": "Discord"})) == {
        "do": "open_app", "name": "Discord"}
    assert P.clean_run([{"do": "click", "name": ""}]) == [] and P.clean_run([{"do": "fly"}]) == []


def _restore_trace(store: Store, *, verdict: bool | None = True) -> str:
    steps = [step("launch_app", app="chrome", inp={"query": "Google Chrome"}, target='window "New Tab - Google Chrome"',
                  result='launched; window "New Tab - Google Chrome" with "Restore pages?" bubble'),
             dict(step("click", app="chrome", verdict=verdict, inp={"element": 41, "read": 3, "expect_hwnd": 9101},
                       target='Button "Restore", window "Restore pages?"', result="clicked Button 'Restore'; 6 tabs"),
                  control="Button|Restore")]
    run = {"source": "agent", "session_id": "S1", "request_id": "worker-1", "lane": "voice", "at": NOW,
           "ended_at": NOW + 9, "words": "open the browser and bring back my pages", "reply": "Restored.",
           "outcome": "final", "steps": steps, "model_calls": 2, "wall_s": 9.0, "numbers": [9101]}
    return P.record_run(store, run)["run_key"]


def test_the_distiller_keeps_a_procedure_runnable_by_the_talker_with_a_grounded_expectation(store):
    key = _restore_trace(store)
    head = P.run_header(store, key)
    ctx = L.Ctx(mode="distill", steps={s.step: s for s in P.run_steps(store, key)}, apps={"chrome"},
                numbers=set(head.signals["numbers"]), run_key=key)
    answer = {"outcome": "worked", "used": [], "operations": [{
        "op": "ADD", "kind": "procedure", "intent": "restore the browser pages after a crash", "apps": ["chrome"],
        "steps": [{"do": "Open Chrome", "from": [1], "expect": "Restore pages?"},
                  {"do": "Click the Button 'Restore' in the bubble", "from": [2],
                   "expect": "the last session's tabs come back"}]}]}   # a paraphrase: not on the screen
    checked = L.check(answer, ctx)
    run = checked.ops[0]["body"]["run"]
    assert run == [{"do": "open_app", "name": "Google Chrome", "expect": "Restore pages?"},
                   {"do": "click", "role": "Button", "name": "Restore"}]
    assert "9101" not in json.dumps(run) and "41" not in json.dumps(run)
    L.apply(store, checked, ctx, embedder=Words(), origin="distill", now=NOW + 200)
    assert P.live(store, ["procedure"])[0].run == run


def test_a_typing_step_fills_the_ways_one_slot_and_never_keeps_the_typed_words(store):
    """Measured on the real distiller (fx2, one pass on a copy): it named the slot ("channel") but not on the type
    step; one slot and one typing step means that step types it."""
    key = P.record_run(store, {"source": "agent", "session_id": "S2", "request_id": "w1", "at": NOW, "ended_at": NOW + 9,
                               "words": "go to the general channel", "outcome": "final", "steps": [
                                   step("hotkey", inp={"keys": ["ctrl", "k"]}, result="the quick switcher opened"),
                                   step("type_text", inp={"text": "general"}, result="typed 7 chars"),
                                   step("press", inp={"key": "enter"}, result='title "#general - Discord"')]})["run_key"]
    ctx = L.Ctx(mode="distill", steps={s.step: s for s in P.run_steps(store, key)}, apps={"discord"}, run_key=key)
    answer = {"outcome": "worked", "used": [], "operations": [{
        "op": "ADD", "kind": "procedure", "intent": "go to a channel in Discord", "apps": ["discord"],
        "slots": ["channel"], "steps": [{"do": "Ctrl+K", "from": [1]}, {"do": "type the channel's name", "from": [2]},
                                        {"do": "Enter", "from": [3], "expect": "#general"}]}]}
    run = L.check(answer, ctx).ops[0]["body"]["run"]
    assert run == [{"do": "keys", "keys": ["ctrl", "k"]}, {"do": "type", "slot": "channel"},
                   {"do": "keys", "keys": ["enter"], "expect": "#general"}]
    assert "general" not in json.dumps(run[:2])


def test_a_procedure_with_a_step_no_hand_can_do_stays_text_only(store):
    key = _restore_trace(store)
    extra = P.record_run(store, {"source": "agent", "session_id": "S1", "request_id": "worker-2", "at": NOW + 50,
                                 "ended_at": NOW + 60, "words": "maximize it", "outcome": "final",
                                 "steps": [step("window_action", app="chrome", inp={"action": "maximize"}),
                                           step("hotkey", app="chrome", inp={"keys": ["ctrl", "t"]})]})["run_key"]
    ctx = L.Ctx(mode="distill", steps={s.step: s for s in P.run_steps(store, extra)}, apps={"chrome"}, run_key=extra)
    answer = {"outcome": "worked", "used": [], "operations": [{
        "op": "ADD", "kind": "procedure", "intent": "maximize the browser and open a tab", "apps": ["chrome"],
        "steps": [{"do": "Maximize the window", "from": [1]}, {"do": "Ctrl+T", "from": [2]}]}]}
    checked = L.check(answer, ctx)
    assert checked.ops and "run" not in checked.ops[0]["body"]
    # a trace step cited twice is not run twice
    ctx2 = L.Ctx(mode="distill", steps={s.step: s for s in P.run_steps(store, key)}, apps={"chrome"}, run_key=key)
    twice = {"outcome": "worked", "used": [], "operations": [{
        "op": "ADD", "kind": "procedure", "intent": "restore the browser pages", "apps": ["chrome"],
        "steps": [{"do": "Open Chrome", "from": [1]}, {"do": "Open it again", "from": [1]}]}]}
    assert "run" not in L.check(twice, ctx2).ops[0]["body"]


# -- 4. the voice uses them -----------------------------------------------------------------------------------------


class DetailMemory:
    """MemoryAccess.call over a client whose practice takes ``detail`` (and no Playbook page is read)."""

    def __init__(self, found: dict[str, Any]) -> None:
        self.asked: list[dict[str, Any]] = []
        self.cards_read = 0
        outer = self

        class Client:
            embedder = SimpleNamespace(warm=lambda allow_local=True: "remote")

            def workflows(self) -> dict:
                outer.cards_read += 1
                return {"workflows": []}

            def practice(self, words: str = "", *, apps: Any = (), open_apps: Any = (), embed_wait_s: Any = None,
                         detail: bool = False) -> dict:
                outer.asked.append({"words": words, "detail": detail})
                return dict(found, measured=bool(words))

        self.client = Client()

    def call(self, what: str, fn: Any, *, timeout_s: float) -> Any:
        return fn(self.client)


FOUND = {
    "ways": [{"id": 9, "ref": "pb:9", "kind": "procedure", "intent": "restore Chrome's pages after a crash",
              "apps": ["chrome"], "text": "1. Open Chrome 2. Click the Button \"Restore\"", "cosine": 0.72,
              "confidence": 0.74, "state": "active", "label": "confidence 0.74", "steps": ["Open Chrome", "Click"],
              "run": RUN, "slots": [], "what": "", "instead": None, "avoid": "reopening tabs one by one"},
             {"id": 12, "ref": "pb:12", "kind": "habit", "intent": "which browser YouTube opens in",
              "apps": ["chrome", "arc"], "text": "YouTube in Chrome most days. Default: Google Chrome.", "cosine": 0.4,
              "confidence": 0.71, "state": "active", "steps": [], "run": [], "slots": [],
              "what": "YouTube in Chrome most days", "instead": None, "default": "Google Chrome", "avoid": None}],
    "pitfalls": [{"id": 4, "ref": "pb:4", "kind": "pitfall", "intent": "volume", "apps": ["chrome"], "cosine": 0.43,
                  "confidence": 0.62, "state": "candidate", "text": "Pressed the media keys. Instead: app_volume",
                  "what": "Pressed the media keys, which move every app", "instead": "app_volume on the app"}],
    "apps": ["chrome"],
}


def test_the_lookup_asks_for_detail_and_shows_executable_steps_as_run_skill_takes_them():
    from yuki.voice.practice import PracticeLookup

    mem = DetailMemory(FOUND)
    lookup = PracticeLookup(mem, apps=lambda: ["chrome.exe"])
    lookup.warm()
    assert lookup.ready.wait(5) and lookup.detail_ok

    async def main() -> Any:
        lookup.new_turn()
        lookup.final("Open Chrome and restore the pages.")
        return await lookup.for_call(2000)

    found, how = asyncio.run(main())
    assert how == "final" and mem.asked[-1]["detail"] is True and mem.cards_read == 0
    assert 'steps=[{"do":"open_app","name":"Google Chrome"},{"do":"click","role":"Button","name":"Restore",' \
           '"expect":"Restore"}]' in found.skills_text and "Not: reopening tabs" in found.skills_text
    assert "Pressed the media keys, which move every app Instead: app_volume on the app" in found.lessons_text
    assert "Default: Google Chrome." in found.lessons_text      # a habit keeps memory's rendering (its default)
    lookup.release()


def test_a_type_step_is_shown_as_the_slot_from_their_words():
    from yuki.voice.practice import run_text

    text = run_text([{"do": "keys", "keys": ["ctrl", "k"]}, {"do": "type", "slot": "channel"},
                     {"do": "keys", "keys": ["enter"]}])
    assert '{"do":"type","text":"<channel, in their words>"}' in text


def test_the_runner_runs_the_ways_own_steps_and_refuses_others():
    from yuki.voice.skills import SkillRefused, fit_to_way, parse_steps

    mine = parse_steps([{"do": "open_app", "name": "Chrome"},
                        {"do": "click", "role": "button", "name": "restore", "expect": "tabs come back"}])
    fitted = fit_to_way(mine, RUN)
    assert [(s.do, s.role, s.name, s.expect) for s in fitted] == [
        ("open_app", "", "Google Chrome", ""), ("click", "Button", "Restore", "Restore")]
    # Chrome already open: the first step left out is fine; a step added, changed or the last one left out is not
    assert [s.name for s in fit_to_way(parse_steps([{"do": "click", "name": "Restore"}]), RUN)] == ["Restore"]
    for bad in ([{"do": "open_app", "name": "Google Chrome"}],
                [{"do": "click", "name": "Restore"}, {"do": "keys", "keys": ["enter"]}],
                [{"do": "open_app", "name": "Google Chrome"}, {"do": "click", "name": "Close"}]):
        with pytest.raises(SkillRefused):
            fit_to_way(parse_steps(bad), RUN)
    # a way without executable steps: the talker's steps as written (v3.18)
    assert fit_to_way(mine, []) is mine


def test_a_way_with_executable_steps_restores_the_pages_on_the_desk():
    from practice_eval import RESTORED, WARP
    from test_voice_procedures import _desk, _hands, _run

    desk = _desk("restore_pages", front=WARP)
    item = {"id": 9, "ref": "pb:9", "kind": "procedure", "intent": "restore Chrome's pages after a crash",
            "apps": ["chrome"], "text": "1. Open Chrome 2. Restore", "run": RUN}
    hands = _hands(desk, words=["Open Chrome and restore the pages."], offered={"pb:9": item})
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:9", "sends": False, "say_when_done": "Pages back.",
                                  "steps": [{"do": "open_app", "name": "Chrome"}, {"do": "click", "name": "restore"}]})
    assert p.spoken == ["Pages back."]
    assert [t["name"] for t in desk.chrome_tabs] == [n for n, _u in RESTORED]


# -- 5. "Open Chrome and restore the pages.": the opening's rest -----------------------------------------------------


class Params:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.function_name, self.arguments, self.tool_call_id = name, args, f"call-{name}"
        self.results: list[tuple[dict[str, Any], Any]] = []
        self.spoken: list[str] = []

        async def push_frame(frame: Any, direction: Any = None) -> None:
            self.spoken.append(getattr(frame, "text", ""))

        self.llm = SimpleNamespace(push_frame=push_frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


def _open(hands: Any, **args: Any) -> Params:
    hands.snapshot()
    params = Params("open_app", args)
    asyncio.run(hands.handle(params))
    return params


def test_the_opening_hands_ask_for_the_rest_of_the_request():
    from yuki.voice.hands import HAND_TOOLS

    for name in ("open_app", "open_url"):
        tool = next(t for t in HAND_TOOLS if t["name"] == name)
        assert "then" in tool["required"] and list(tool["properties"]).index("then") < list(
            tool["properties"]).index("say_when_done")


def test_an_opening_with_a_rest_gives_the_talker_its_turn_back_with_the_settled_screen(monkeypatch):
    import yuki.voice.hands as hmod
    from practice_eval import WARP
    from test_voice_procedures import Log, _desk, _hands

    monkeypatch.setattr(hmod, "SETTLE_QUIET_S", 0.2)
    desk = _desk("restore_pages", front=WARP)              # Chrome closed after a crash: the bubble comes with it
    hands = _hands(desk, words=["Open Chrome and restore the pages."])
    hands.log = Log()
    p = _open(hands, say_first="Opening Chrome.", name="Google Chrome", then="restore the pages",
              say_when_done="Done, your pages are back.")
    result, props = p.results[0]
    assert p.spoken == []                                   # no done line for half of the request
    assert result["then"] == "restore the pages" and "Still to do" in result["how_to_handle_it"]
    assert props is None or getattr(props, "run_llm", None) is not False   # the talker gets its next call
    settle = hands.log.of("hands", op="open_settle")[0]
    assert settle["settled"] is True and settle["settle_ms"] >= 200 and settle["popups"] >= 0
    # the talker's next call sees Chrome and the bubble's items (to click Restore by its number)
    snap = hands.snapshot()
    assert "Restore" in snap.text
    assert [c[0] for c in desk.calls] == ["launch_app"]


def test_an_opening_that_is_the_whole_request_still_ends_with_its_done_line():
    from practice_eval import WARP
    from test_voice_procedures import _desk, _hands

    desk = _desk("restore_pages", front=WARP, crashed=False)
    hands = _hands(desk, words=["Open Chrome."])
    p = _open(hands, say_first="Opening Chrome.", name="Google Chrome", then="", say_when_done="Chrome's up.")
    result, props = p.results[0]
    assert p.spoken == ["Chrome's up."] and getattr(props, "run_llm", None) is False and "then" not in result


# -- 6. reads to the reader lane even while the worker is idle (30b §2) ---------------------------------------------


class _Agent:
    tool_names = None
    last_summary = None
    awaiting_answer = False
    cancelled = False

    def run(self, request: str, **_: Any) -> Any:
        return iter(())

    def answer(self, text: str) -> None:
        pass

    def cancel(self, reason: str = "cancelled") -> None:
        pass

    def take_steers(self) -> list:
        return []


def _runtime(tmp_path: Path) -> Any:
    import test_voice_v39  # noqa: F401 - the Qt application

    from yuki.config import Settings
    from yuki.ui.runtime import AgentRuntime
    from yuki.ui.uilog import UiLog

    return AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                        UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=_Agent(),
                        front_desk_agent=_Agent(), memory=SimpleNamespace(session_id="t"))


def test_a_read_goes_to_the_reader_lane_while_it_is_free_and_nothing_else_changes(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.submit("what is on my calendar", source="voice", needs_hands=False)[0] == "front_desk"
    # the reader is busy now: the next read goes to the (idle) worker, as lanes.route says
    assert rt.submit("what did I do yesterday", source="voice", needs_hands=False)[0] == "worker"
    rt2 = _runtime(tmp_path / "b")
    assert rt2.submit("open my notes", source="voice")[0] == "worker"                    # no mark: as before
    assert rt2.submit("anything", source="text", needs_hands=None)[0] == "front_desk"   # worker busy: as before
    rt3 = _runtime(tmp_path / "c")
    assert rt3.submit("close it", source="voice", lane="worker", needs_hands=False)[0] == "worker"   # lane wins
    # a pack is only for the worker taking it now (never handed to the reader)
    rt4 = _runtime(tmp_path / "d")
    rt4.submit("what is on my calendar", source="voice", needs_hands=False, pack=object())
    assert rt4.front_desk._jobs.get_nowait().pack is None


def test_the_bridge_passes_the_talkers_only_finds_out_mark_when_the_runtime_takes_it():
    import test_voice_v39  # noqa: F401 - the Qt application
    from test_voice_v39 import FakeRuntime

    class ReaderRuntime(FakeRuntime):
        def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
                   needs_hands: Any = None, **kw: Any) -> tuple[str, int]:
            kw.update({k: v for k, v in (("lane", lane), ("needs_hands", needs_hands)) if v is not None})
            return super().submit(text, origin_hwnd=origin_hwnd, source=source, **kw)

    async def main() -> tuple[Any, Any]:
        from yuki.voice.bridge import VoiceBridge

        rt = ReaderRuntime()
        bridge = VoiceBridge(rt, foreground=lambda: 42)
        bridge.attach(asyncio.get_running_loop())
        bridge._submit(bridge._new_job("what is on my calendar"), "what is on my calendar", None, needs_hands=False)
        bridge._submit(bridge._new_job("close it"), "close it", None, lane="worker", needs_hands=False)
        old = FakeRuntime()
        bridge2 = VoiceBridge(old, foreground=lambda: 42)
        bridge2.attach(asyncio.get_running_loop())
        bridge2._submit(bridge2._new_job("what is on my calendar"), "what", None, needs_hands=False)
        return rt, old

    rt, old = asyncio.run(main())
    assert rt.submitted[0][2].get("needs_hands") is False
    assert rt.submitted[1][2].get("lane") == "worker" and "needs_hands" not in rt.submitted[1][2]
    assert "needs_hands" not in old.submitted[0][2]          # a runtime without it is never sent it
