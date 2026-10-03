"""Voice v3.18 (docs/research/revamp-2026-10-02/29-build-voice-procedures.md): the talker uses what practice memory
learned. Hand traces into practice memory (off the voice loop), the Lessons and Learned ways lines in the talker's
volatile block (measured floors, at most 400 characters of lessons), learned ways run as the talker's hand step by step
with every step checked and the thinker taking over at the first deviation (never a send the user did not plainly ask
for), the talker block logged, the personal brief hook, the request-pack hook, Yuki's own voice on the app volumes line.

Offline only: stand-in desks (``scripts/practice_eval.py``'s stateful desk), stand-in memory or a temp database; no
microphone, no speakers, no real desktop, no real memory, no model call.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication

from test_voice_v316 import _no_real_audio_devices  # noqa: F401 - rule 7: every sounddevice stream fails the test
from test_voice_v39 import FakeRuntime, _bridge, _settle
from yuki.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

APP = QCoreApplication.instance() or QCoreApplication([])


# -- stand-ins ---------------------------------------------------------------------------------------------------

class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.log_dir = None

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        self.events.append((kind, fields))
        return fields

    def turn(self, row: dict[str, Any]) -> None:
        self.events.append(("turn", row))

    def of(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        return [f for k, f in self.events if k == kind and all(f.get(a) == b for a, b in match.items())]


class TraceMemory:
    """MemoryAccess's trace writers, recording what they got and on which thread."""

    session_id = "s-test"

    def __init__(self) -> None:
        self.hand_traces: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.threads: list[str] = []

    def record_hand_trace(self, **kw: Any) -> bool:
        self.threads.append(threading.current_thread().name)
        self.hand_traces.append(kw)
        return True

    def record_run(self, run: dict[str, Any]) -> bool:
        self.threads.append(threading.current_thread().name)
        self.runs.append(run)
        return True


def _drain() -> None:
    """Wait until the voice layer's ordered worker has run everything queued before now."""
    from yuki.voice.workers import ordered

    done = threading.Event()
    ordered().submit(done.set)
    assert done.wait(5.0)


class Params:
    def __init__(self, name: str, args: dict[str, Any], heard: str = "") -> None:
        self.function_name, self.arguments, self.tool_call_id = name, args, f"call-{name}"
        self.results: list[dict[str, Any]] = []
        self.spoken: list[str] = []
        self.context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": heard}] if heard else [])

        async def push_frame(frame: Any, direction: Any = None) -> None:
            self.spoken.append(getattr(frame, "text", ""))

        self.llm = SimpleNamespace(push_frame=push_frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append(result)


class EscalateBridge:
    """The bridge as a hand sees it: escalate records what the thinker would be told."""

    def __init__(self) -> None:
        self.escalated: list[dict[str, Any]] = []
        self.busy_tools = 0
        self.gate = None

    def desk_job(self) -> None:
        return None

    def may_ack(self) -> bool:
        return False

    async def escalate(self, params: Any, name: str, args: dict, report: str, acked: bool = False,
                       context: str | None = None) -> bool:
        self.escalated.append({"name": name, "report": report, "context": context})
        return True

    async def _ack_if_silent(self, params: Any, args: dict) -> None:
        return None


def _desk(name: str = "volume_yuki", **over: Any) -> Any:
    from practice_eval import SCENARIOS
    from voice_procedures_eval import VoiceDesk

    sc = dict(next(s for s in SCENARIOS if s["name"] == name), **over)
    return VoiceDesk(sc)


def _hands(desk: Any, *, words: list[str] | None = None, shown: list[int] | None = None, memory: Any = None,
           bridge: Any = None, offered: dict[str, Any] | None = None) -> Any:
    from yuki.voice.hands import Hands

    hands = Hands(desk)
    hands.memory = memory if memory is not None else TraceMemory()
    hands.words = lambda: list(words or [])
    hands.turn_key = lambda: "s1-3"
    hands.shown = lambda: list(shown or [])
    hands.user_turn = lambda: 3
    hands.bridge = bridge
    if offered is not None:
        hands.practice = SimpleNamespace(offered=lambda ref, turn: offered.get(str(ref)))
    return hands


def _run(hands: Any, name: str, args: dict[str, Any], heard: str = "") -> Params:
    hands.snapshot()
    params = Params(name, args, heard)
    asyncio.run(hands.handle(params))
    return params


# -- 1. hand traces --------------------------------------------------------------------------------------------

def test_a_hand_is_kept_as_a_trace_off_the_loop_with_its_target_its_verdict_and_the_users_words() -> None:
    """The 2 Oct volume mistake was a hand's (media_key on the system volume): the learners now see it, with what it
    moved said plainly, the user's words, and a request id of the voice turn's own."""
    from practice_eval import CHROME

    desk = _desk(front=CHROME)
    hands = _hands(desk, words=["Lower your volume."])
    started = time.time()
    p = _run(hands, "media_key", {"key": "volumedown", "times": 2, "say_when_done": "Turned it down."})
    assert p.spoken == ["Turned it down."] and desk.mixer.system == 56.0
    _drain()
    mem = hands.memory
    assert len(mem.hand_traces) == 1 and not mem.runs
    t = mem.hand_traces[0]
    assert t["name"] == "media_key" and t["ok"] is True and t["verified"] is True
    assert t["target"] == "the system volume (a media key: every app at once)" and t["app"] is None
    assert t["words"] == "Lower your volume." and t["request_id"].startswith("voice-s1-3-h")
    assert "volume is now" in t["report"] and started - 1 <= t["at"] <= time.time()
    assert mem.threads == ["yuki-voice-ordered_0"]   # never on the voice loop
    assert hands.traces_queued == 1


def test_an_app_volume_hand_names_the_app_and_a_click_names_the_control_by_role_and_name() -> None:
    from practice_eval import CHROME

    desk = _desk(front=CHROME)
    hands = _hands(desk, words=["turn your volume down"])
    _run(hands, "app_volume", {"app": "Yuki.exe", "change": -20, "say_when_done": "Turned myself down."})
    _drain()
    t = hands.memory.hand_traces[-1]
    assert t["app"] == "Yuki.exe" and t["target"] == "the mixer volume of Yuki.exe (that app only)"
    assert desk.mixer.levels["Yuki.exe"] == 80.0 and desk.mixer.system == 60.0
    # a click: the control by role and name, in the window the talker was shown
    desk2 = _desk("restore_pages", open=[CHROME], front=CHROME)
    hands2 = _hands(desk2, words=["restore the pages"])
    hands2.snapshot()
    hands2._read_items(CHROME, desk2.title(CHROME), own=True)
    hands2.snapshot()
    number = next(i for i, it in enumerate(hands2.items.items, 1) if it["name"] == "Restore")
    _run(hands2, "click", {"window": CHROME, "item": number, "say_when_done": ""})
    _drain()
    t = hands2.memory.hand_traces[-1]
    assert t["name"] == "click" and t["target"].startswith('Button "Restore"') and t["app"] == "chrome.exe"


def test_a_hand_shown_lessons_is_kept_through_record_run_with_them_as_injected() -> None:
    from practice_eval import CHROME

    desk = _desk(front=CHROME)
    hands = _hands(desk, words=["lower your volume"], shown=[4, 12])
    _run(hands, "app_volume", {"app": "Yuki.exe", "change": -10, "say_when_done": ""})
    _drain()
    mem = hands.memory
    assert not mem.hand_traces and len(mem.runs) == 1
    run = mem.runs[0]
    assert run["source"] == "hand" and run["injected"] == [4, 12] and run["session_id"] == "s-test"
    assert run["steps"][0]["tool"] == "app_volume" and run["steps"][0]["verdict"] is True
    assert '"app":"Yuki.exe"' in run["steps"][0]["input"] and run["words"] == "lower your volume"


def test_a_refused_hand_did_nothing_and_leaves_no_trace() -> None:
    from practice_eval import CHROME

    desk = _desk(front=CHROME)
    hands = _hands(desk, words=["click the third one"])
    _run(hands, "click", {"window": CHROME, "item": 99, "say_when_done": ""})
    _drain()
    assert hands.memory.hand_traces == [] and hands.memory.runs == []


def test_a_hand_trace_reaches_practice_memory_and_the_learner_sees_the_users_next_words(tmp_path: Path) -> None:
    """End to end on a temp database: the hand's run is kept (target, app, verdict), and the learner's follow-ups are
    the user's words after it - not the hand's own turn, which is dated when it opened (v3.18)."""
    from practice_eval import CHROME

    from yuki.agent.memory import MemoryAccess
    from yuki.memory import practice as P
    from yuki.memory.practice_learn import followups
    from yuki.memory.store import Store

    db = tmp_path / "memory" / "memory.db"
    memory = MemoryAccess(path=db, session_id="voice-e2e")
    t0 = time.time()
    # the user's turn, dated when it opened (before the hand acted on it)
    memory.log_turn(request_id="voice-s1-3", at=t0 - 2.0, user_text="Lower your volume.", reply_text="Done.",
                    actions=["media_key (by voice)"], outcome="voice")
    desk = _desk(front=CHROME)
    hands = _hands(desk, words=["Lower your volume."], memory=memory)
    _run(hands, "media_key", {"key": "volumedown", "say_when_done": "Done."})
    _drain()
    memory.log_turn(request_id="voice-s1-4", at=time.time() + 0.5, outcome="voice", actions=[],
                    user_text="No. No. No. Not my system's audio. I want you to turn your volume down.", reply_text="")
    assert memory.flush_turns(10.0)
    store = Store.open(db)
    try:
        heads = P.runs_between(store, t0 - 60)
        assert len(heads) == 1 and heads[0].source == "hand" and heads[0].lane == "voice"
        steps = P.run_steps(store, heads[0].run_key)
        assert steps[0].tool == "media_key" and steps[0].verdict is True
        assert steps[0].target == "the system volume (a media key: every app at once)"
        follow = followups(store, heads[0])
        assert [f["words"] for f in follow] == ["No. No. No. Not my system's audio. I want you to turn your volume "
                                                "down."]
    finally:
        store.close()


def test_a_voice_turn_is_logged_with_the_time_it_opened(tmp_path: Path) -> None:
    from yuki.voice.pipeline import TurnObserver, _Turn

    got: dict[str, Any] = {}
    memory = SimpleNamespace(log_turn=lambda **kw: got.update(kw))
    obs = TurnObserver(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), log=Log(),
                       memory=memory, session="s1", speech_end=lambda: None, on_state=lambda *a: None,
                       tts_provider="deepgram", activity=lambda: None)
    obs.turn = _Turn(n=3, trigger="user", opened=time.monotonic() - 5.0, user_text="Lower your volume.")
    obs.close()
    assert got["request_id"] == "voice-s1-3" and abs(got["at"] - (time.time() - 5.0)) < 0.5


# -- 2. the lessons and learned-ways lines ---------------------------------------------------------------------

def _pit(pid: int, cosine: float | None, text: str = "", instead: str = "", conf: float = 0.62) -> tuple[dict, dict]:
    item = {"id": pid, "ref": f"pb:{pid}", "kind": "pitfall", "intent": f"lesson {pid}", "cosine": cosine,
            "confidence": conf, "state": "candidate", "apps": ["chrome"]}
    card = {"id": f"pb:{pid}", "kind": "pitfall", "title": f"lesson {pid}",
            "text": f"{text} Instead: {instead}" if instead else text, "instead": instead or None}
    return item, card


def test_the_lessons_line_keeps_what_to_do_instead_and_stays_within_400_characters() -> None:
    from yuki.voice.practice import LESSONS_CHARS, lessons_block, lesson_parts

    long_what = "Pressed the media keys, which moved the system volume of every app, " * 4
    items = [_pit(4, 0.43, long_what, "app_volume on your own voice's entry, never the media keys"),
             _pit(8, 0.30, "Typed with the clipboard: the mention stayed plain text " * 3, "type @ and the name by keys"),
             _pit(9, 0.28, "A third lesson " * 10, "the third fix")]
    text, shown = lessons_block(items)
    assert len(text) <= LESSONS_CHARS and [i["id"] for i in shown] == [4, 8, 9]
    assert "Instead: app_volume on your own voice's entry, never the media keys" in text
    assert "Instead: type @ and the name by keys" in text and "Instead: the third fix" in text
    assert text.count("Instead:") == 3   # memory's rendering already ends with it: never twice
    assert lesson_parts(*items[0])[1] == "app_volume on your own voice's entry, never the media keys"
    assert lessons_block([]) == ("", [])


def test_a_skill_needs_both_measured_floors_and_a_state_memory_applies_without_a_look() -> None:
    from yuki.voice.practice import SKILL_MIN_CONFIDENCE, SKILL_MIN_COSINE, qualifies

    base = {"kind": "procedure", "cosine": 0.72, "confidence": 0.74, "state": "active"}
    assert qualifies(base)
    assert not qualifies(dict(base, cosine=SKILL_MIN_COSINE - 0.01))
    assert not qualifies(dict(base, confidence=SKILL_MIN_CONFIDENCE - 0.01))
    assert not qualifies(dict(base, state="candidate")) and not qualifies(dict(base, state="stale"))
    assert not qualifies(dict(base, kind="habit")) and not qualifies(dict(base, cosine=None))
    assert qualifies(dict(base, state="trusted"))
    # one run can never reach the confidence floor: (1 + 1) / (1 + 2)
    assert 2 / 3 < SKILL_MIN_CONFIDENCE < 3 / 4


class FakePracticeMemory:
    """MemoryAccess.call over a client with practice / workflows / embedder (canned answers)."""

    def __init__(self, found: dict[str, Any], *, embed: str = "remote", cards: list[dict] | None = None,
                 has_practice: bool = True) -> None:
        self.asked: list[tuple[str, list[str]]] = []
        outer = self

        class Client:
            embedder = SimpleNamespace(warm=lambda allow_local=True: embed)

            def workflows(self) -> dict:
                return {"workflows": cards or []}

        client = Client()
        if has_practice:
            def practice(words: str = "", *, apps: Any = (), open_apps: Any = (), embed_wait_s: Any = None) -> dict:
                outer.asked.append((words, list(open_apps)))
                return dict(found, measured=bool(words))
            client.practice = practice
        self.client = client

    def call(self, what: str, fn: Any, *, timeout_s: float) -> Any:
        return fn(self.client)


FOUND = {
    "ways": [{"id": 9, "ref": "pb:9", "kind": "procedure", "intent": "restore Chrome's pages after a crash",
              "apps": ["chrome"], "text": "1. Click the Button \"Restore\" in the \"Restore pages?\" bubble",
              "cosine": 0.72, "confidence": 0.74, "state": "active", "label": "confidence 0.74"},
             {"id": 12, "ref": "pb:12", "kind": "habit", "intent": "which browser YouTube opens in",
              "apps": ["chrome", "arc"], "text": "YouTube in Chrome, Arc now and then", "cosine": 0.40,
              "confidence": 0.71, "state": "active"}],
    "pitfalls": [{"id": 4, "ref": "pb:4", "kind": "pitfall", "intent": "volume", "apps": ["chrome"], "cosine": 0.43,
                  "confidence": 0.62, "state": "candidate", "text": "Pressed the media keys. Instead: app_volume"},
                 {"id": 8, "ref": "pb:8", "kind": "pitfall", "intent": "warp restore button", "apps": ["warp"],
                  "cosine": 0.05, "confidence": 0.63, "state": "candidate", "text": "unrelated"}],
    "apps": ["chrome", "warp"],
}


def test_the_lookup_offers_a_qualifying_way_and_only_the_lessons_that_bear_on_the_words() -> None:
    from yuki.voice.practice import PracticeLookup

    mem = FakePracticeMemory(FOUND)
    lookup = PracticeLookup(mem, apps=lambda: ["chrome.exe", "Warp.exe"])
    lookup.warm()
    assert lookup.ready.wait(5)

    async def main() -> Any:
        lookup.new_turn()
        lookup.final("Restore the pages.")
        return await lookup.for_call(2000)

    found, how = asyncio.run(main())
    assert how == "final" and mem.asked[-1] == ("Restore the pages.", ["chrome.exe", "Warp.exe"])
    assert [s["id"] for s in found.skills] == [9] and 'pb:9 "restore Chrome\'s pages after a crash"' in found.block
    assert found.kinds_ids() == "pitfall:4;habit:12;skill:9"   # the Warp lesson (cosine 0.05) is not shown
    assert found.ids() == [4, 12, 9]
    facts = found.facts()
    assert facts["chars"] == len(found.block) and "Restore" not in str(facts)   # ids and numbers only
    lookup.offer(found, 3)
    assert lookup.offered("pb:9", 3)["id"] == 9 and lookup.offered("9", 4)["id"] == 9
    assert lookup.offered("pb:9", 5) is None and lookup.offered("pb:12", 3) is None   # a habit is no skill
    lookup.release()


def test_without_a_service_embedder_the_words_are_not_embedded_here_and_no_way_is_offered() -> None:
    from yuki.voice.practice import PracticeLookup

    mem = FakePracticeMemory(dict(FOUND, ways=[]), embed="none")
    lookup = PracticeLookup(mem, apps=lambda: ["chrome.exe"])
    lookup.warm()
    assert lookup.ready.wait(5) and lookup.embed_ok is False

    async def main() -> Any:
        lookup.final("Restore the pages.")
        return await lookup.for_call(2000)

    found, _how = asyncio.run(main())
    assert mem.asked[-1][0] == "" and found.skills == [] and found.measured is False
    # an older memory without practice: nothing at all
    old = PracticeLookup(FakePracticeMemory(FOUND, has_practice=False), apps=lambda: [])
    old.warm()
    assert old.ready.wait(5) and old.disabled
    assert asyncio.run(old.for_call(10)) == (None, "off")


def test_the_practice_lines_ride_in_the_volatile_block_before_the_desktop() -> None:
    from yuki.voice.mouth import DESK_LABEL, LOOKUP_LABEL, volatile_block

    text = volatile_block(lookup="- (fact) x", desk="[1] chrome.exe", practice="[Lessons (yours...): - y]")["text"]
    assert text.index(LOOKUP_LABEL) < text.index("[Lessons") < text.index(DESK_LABEL)
    assert "[Lessons" not in volatile_block(desk="[1] chrome.exe")["text"]


def test_the_talker_block_is_logged_by_kinds_ids_and_size_never_its_text(tmp_path: Path) -> None:
    from yuki.voice.log import TURN_COLUMNS
    from yuki.voice.pipeline import LlmCall, TurnObserver, _Hooks, _Turn
    from yuki.voice.practice import TalkerPractice

    for col in ("practice", "practice_wait_ms", "talker_block", "talker_block_chars"):
        assert col in TURN_COLUMNS
    log = Log()
    found = TalkerPractice(words="Restore the pages.", turn=1, started=time.perf_counter(), found=FOUND)
    found.skills = [FOUND["ways"][0]]
    found.lessons = [FOUND["pitfalls"][0]]
    found.lessons_text, found.skills_text = "[Lessons: secret words]", "[Learned ways: pb:9 ...]"
    offered: list[Any] = []
    conv = SimpleNamespace(log=log, session="s1", observer=SimpleNamespace(user_turns=3), _shown_ids=[],
                           practice=SimpleNamespace(offer=lambda f, turn: offered.append((f, turn))))
    call = LlmCall(entered=time.monotonic())
    _Hooks(conv)._practice(call, found, "final", 12.34)
    assert call.practice == "final" and call.practice_wait_ms == 12.3 and call.talker_block == "pitfall:4;skill:9"
    assert call.practice_block == found.block and call.talker_block_chars == len(found.block)
    assert conv._shown_ids == [4, 9] and offered == [(found, 3)]
    event = log.of("practice", op="block")[0]
    assert event["block"] == "pitfall:4;skill:9" and "secret" not in str(event)
    obs = TurnObserver(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"), log=log,
                       memory=None, session="s1", speech_end=lambda: None, on_state=lambda *a: None,
                       tts_provider="deepgram", activity=lambda: None)
    obs.turn = _Turn(n=1, trigger="user", opened=time.monotonic(), user_text="Restore the pages.", llm=call)
    obs.close()
    row = log.of("turn")[0]
    assert row["talker_block"] == "pitfall:4;skill:9" and row["talker_block_chars"] == len(found.block)
    assert row["practice"] == "final" and "secret" not in str(row)


# -- 3. learned ways run as the talker's hand ---------------------------------------------------------------------

RESTORE = {"id": 9, "ref": "pb:9", "kind": "procedure", "intent": "restore Chrome's pages after a crash",
           "apps": ["chrome"], "cosine": 0.72, "confidence": 0.74, "state": "active",
           "text": "1. Open Chrome (open_app Google Chrome): its window comes up 2. Click the Button \"Restore\" in the "
                   "\"Restore pages?\" bubble: the last session's tabs come back"}
CHANNEL = {"id": 10, "ref": "pb:10", "kind": "procedure", "intent": "go to a Discord channel by name",
           "apps": ["discord"], "cosine": 0.72, "confidence": 0.74, "state": "active",
           "text": "1. Press Ctrl+K: the quick switcher opens 2. Type the channel's name 3. Press Enter: the channel "
                   "opens (changes each time: channel)"}
RESTORE_STEPS = [{"do": "open_app", "name": "Google Chrome"}, {"do": "click", "role": "Button", "name": "Restore"}]


def test_a_learned_way_runs_step_by_step_verified_said_once_and_kept_as_a_cited_run() -> None:
    from practice_eval import RESTORED, WARP

    desk = _desk("restore_pages", front=WARP)   # Chrome closed after a crash; the bubble waits
    hands = _hands(desk, words=["Restore the pages."], shown=[4], offered={"pb:9": RESTORE})
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:9", "steps": RESTORE_STEPS, "sends": False,
                                  "say_when_done": "Your pages are back."})
    assert p.spoken == ["Your pages are back."] and "done" in p.results[0]
    assert [t["name"] for t in desk.chrome_tabs] == [n for n, _u in RESTORED]
    assert [c[0] for c in desk.calls] == ["launch_app", "click"]
    _drain()
    run = hands.memory.runs[-1]
    assert run["outcome"] == "final" and run["cited"] == [9] and run["injected"] == [4, 9]
    assert [s["tool"] for s in run["steps"]] == ["launch_app", "click"]
    assert all(s["playbook"] == "pb:9" for s in run["steps"]) and run["steps"][1]["target"].startswith('Button "Restore"')
    assert hands.memory.hand_traces == []   # one run for the whole way, not a trace per step


def test_a_learned_way_that_deviates_stops_and_the_thinker_carries_on_from_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome did not crash this time: no "Restore pages?" bubble. Step 1 ran; step 2's control is not on the screen:
    the run stops, nothing more is pressed, and the thinker gets the request with what was done and where it stopped."""
    import yuki.voice.skills as skills
    from practice_eval import WARP

    monkeypatch.setattr(skills, "AFTER_OPEN_WAIT_S", 0.3)
    desk = _desk("restore_pages", front=WARP, crashed=False)
    bridge = EscalateBridge()
    hands = _hands(desk, words=["Open Chrome and restore the pages."], offered={"pb:9": RESTORE}, bridge=bridge)
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:9", "steps": RESTORE_STEPS, "sends": False,
                                  "say_when_done": "Your pages are back."})
    assert p.spoken == [] and p.results[0].get("passed_on") is True
    assert [c[0] for c in desk.calls] == ["launch_app"]          # nothing after the deviation
    ctx = bridge.escalated[0]["context"]
    assert "step 2" in ctx and 'Button "Restore"' in ctx and "do not redo" in ctx and "open the app" in ctx
    assert hands.halted                                          # the talker's hands wait for the user's next words
    _drain()
    run = hands.memory.runs[-1]
    assert run["outcome"] == "handed_over" and [s["kind"] for s in run["steps"]] == ["action", "read"]
    assert run["steps"][1]["ok"] is False


def test_keys_type_and_keys_with_an_expectation_switch_the_channel_and_send_nothing() -> None:
    from practice_eval import DISCORD

    desk = _desk("discord_tag", front=DISCORD)
    hands = _hands(desk, words=["go to the general channel"], offered={"pb:10": CHANNEL})
    steps = [{"do": "keys", "keys": ["ctrl", "k"]}, {"do": "type", "text": "general"},
             {"do": "keys", "keys": ["enter"], "expect": "general"}]
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:10", "steps": steps, "sends": False,
                                  "say_when_done": "There."})
    assert p.spoken == ["There."] and desk.discord_view == "general" and desk.sent == []
    assert [c[0] for c in desk.calls] == ["hotkey", "type_text", "hotkey"]


def test_a_key_never_follows_a_step_whose_effect_could_not_be_seen() -> None:
    from practice_eval import DISCORD

    desk = _desk("discord_tag", front=DISCORD)
    bridge = EscalateBridge()
    hands = _hands(desk, words=["go to the general channel"], offered={"pb:10": CHANNEL}, bridge=bridge)
    steps = [{"do": "scroll", "direction": "down"}, {"do": "keys", "keys": ["ctrl", "k"]}]
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:10", "steps": steps, "sends": False,
                                  "say_when_done": "There."})
    assert p.spoken == [] and "hotkey" not in [c[0] for c in desk.calls]
    assert "could not be seen" in bridge.escalated[0]["context"]


def test_keys_go_only_to_a_window_of_the_ways_own_app() -> None:
    from practice_eval import CHROME

    desk = _desk("discord_tag", front=CHROME)
    bridge = EscalateBridge()
    hands = _hands(desk, words=["go to the general channel"], offered={"pb:10": CHANNEL}, bridge=bridge)
    p = _run(hands, "run_skill", {"say_first": "", "skill": "pb:10", "steps": [{"do": "keys", "keys": ["ctrl", "k"]}],
                                  "sends": False, "say_when_done": ""})
    assert desk.calls == [] and p.results[0].get("passed_on") is True
    assert "not the way's app" in bridge.escalated[0]["context"]


def test_a_way_that_was_not_offered_or_cannot_run_as_written_does_nothing_and_goes_to_the_thinker() -> None:
    from practice_eval import DISCORD

    desk = _desk("discord_tag", front=DISCORD)
    bridge = EscalateBridge()
    hands = _hands(desk, words=["go to the general channel"], offered={"pb:10": CHANNEL}, bridge=bridge)
    steps = [{"do": "keys", "keys": ["ctrl", "k"]}, {"do": "type", "text": "general"}]
    _run(hands, "run_skill", {"say_first": "", "skill": "pb:77", "steps": steps, "sends": False, "say_when_done": ""})
    _run(hands, "run_skill", {"say_first": "", "skill": "pb:10", "steps": [{"do": "type", "text": "hello everyone"}],
                              "sends": False, "say_when_done": ""})   # words they never said
    _run(hands, "run_skill", {"say_first": "", "skill": "pb:10", "steps": [{"do": "wiggle"}], "sends": False,
                              "say_when_done": ""})
    assert desk.calls == [] and len(bridge.escalated) == 3
    assert "not one offered" in bridge.escalated[0]["context"]
    assert "neither their words nor the learned way's own" in bridge.escalated[1]["context"]
    _drain()
    assert hands.memory.runs == [] and hands.memory.hand_traces == []   # nothing ran: nothing to learn from


def test_never_a_send_unless_their_words_plainly_said_what_goes_to_whom() -> None:
    from yuki.voice.skills import check_sends, check_texts, parse_steps

    said = ["tell Hemanth I'm on in ten"]
    assert check_sends({"sends": False}, said) is None
    assert check_sends({"sends": True, "to": "Hemanth", "what": "I'm on in ten"}, said) is None
    assert "do not plainly say" in check_sends({"sends": True, "to": "Hemanth", "what": ""}, ["message Hemanth"])
    assert "not what they said" in check_sends({"sends": True, "to": "Hemanth", "what": "see you soon"}, said)
    assert "not what they said" in check_sends({"sends": True, "to": "Elina", "what": "I'm on in ten"}, said)
    steps = parse_steps([{"do": "type", "text": "I'm on in ten"}, {"do": "keys", "keys": ["enter"]}])
    assert check_texts(steps, said, "") is None
    assert check_texts(parse_steps([{"do": "type", "text": "I'm on in five"}]), said, "") is not None
    # the way's own words may be typed (a fixed part of it), never words nobody said
    assert check_texts(parse_steps([{"do": "type", "text": "general"}]), ["go there"], "Type general and Enter") is None
    with pytest.raises(Exception):
        parse_steps([{"do": "keys", "keys": []}])


def test_a_sending_way_without_their_plain_words_is_not_run() -> None:
    from practice_eval import DISCORD

    desk = _desk("discord_tag", front=DISCORD, open=[DISCORD])
    desk.discord_view = "general"
    bridge = EscalateBridge()
    item = dict(CHANNEL, id=13, ref="pb:13", intent="message a channel", text="1. Type the message 2. Press Enter")
    hands = _hands(desk, words=["message the general channel"], offered={"pb:13": item}, bridge=bridge)
    steps = [{"do": "type", "text": "message the general channel"}, {"do": "keys", "keys": ["enter"]}]
    _run(hands, "run_skill", {"say_first": "", "skill": "pb:13", "steps": steps, "sends": True, "to": "general channel",
                              "what": "", "say_when_done": ""})
    assert desk.calls == [] and desk.sent == [] and "do not plainly say" in bridge.escalated[0]["context"]


# -- 4. the talker's prompt, its tools, its own voice -------------------------------------------------------------

def test_run_skill_is_a_hand_that_speaks_first_and_the_prompt_teaches_lessons_ways_and_the_own_voice() -> None:
    from yuki.voice.hands import HAND_TOOLS
    from yuki.voice.mouth import HANDS_PROMPT
    from yuki.voice.pipeline import SAY_FIRST_TOOLS, early_words

    tool = next(t for t in HAND_TOOLS if t["name"] == "run_skill")
    assert list(tool["properties"])[0] == "say_first" and "run_skill" in SAY_FIRST_TOOLS
    assert set(tool["required"]) >= {"skill", "steps", "sends", "say_when_done"}
    assert early_words('{"say_first": "Restoring them.", "skill": "pb:9"') == "Restoring them."
    for words in ("Lessons line", "Learned ways line", "run_skill", "(your own voice)", "sends"):
        assert words in HANDS_PROMPT
    assert "pb:9" not in HANDS_PROMPT and "pb:<n>" in HANDS_PROMPT   # placeholders: nothing concrete to copy (v3.17)


def test_yukis_own_voice_is_marked_on_the_app_volumes_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.voice.hands as hmod

    line = hmod.audio_line_own([{"app": "chrome.exe", "level": 80, "playing": True},
                                {"app": "Yuki.exe", "level": 100, "playing": True, "own": True}])
    assert line.endswith("Yuki.exe 100% playing (your own voice)") and "chrome.exe 80% playing;" in line
    # the real mixer: this process's own entry is marked by its process name
    desk = SimpleNamespace(audio_apps=lambda: [{"app": "Yuki.exe", "level": 100.0, "muted": False, "playing": True},
                                               {"app": "chrome.exe", "level": 80.0, "muted": False, "playing": True}])
    hands = hmod.Hands.__new__(hmod.Hands)
    hands.backend = desk
    monkeypatch.setattr(hmod, "_is_real_backend", lambda backend: True)
    monkeypatch.setattr(hmod, "_own_process_name", lambda: "Yuki.exe")
    assert "Yuki.exe 100% playing (your own voice)" in hands._audio_line()
    monkeypatch.setattr(hmod, "_is_real_backend", lambda backend: False)
    assert "(your own voice)" not in hands._audio_line()   # a stand-in desk marks its own entries


# -- 5. the personal brief and the request pack -------------------------------------------------------------------

def test_the_personal_brief_replaces_the_personality_and_persona_when_memory_has_it() -> None:
    from yuki.voice.mouth import MouthMemory, PROFILE_LABEL, fetch_brief, mouth_system_blocks

    asked: list[str] = []

    def call(what: str, fn: Any, timeout_s: float) -> Any:
        return fn(SimpleNamespace(brief_for=lambda audience: asked.append(audience) or "Talk to Sam like a friend."))

    brief = fetch_brief(SimpleNamespace(call=call))
    assert brief == "Talk to Sam like a friend." and asked == ["talker"]
    assert fetch_brief(SimpleNamespace(call=lambda what, fn, timeout_s: fn(SimpleNamespace()))) is None
    mem = MouthMemory(names=["Sam"], personality="Personality: blunt.", persona="Short and dry.", source="memory")

    def profile(m: MouthMemory) -> str:
        return next(b["text"] for b in mouth_system_blocks(m) if b["text"].startswith(PROFILE_LABEL))

    with_brief = profile(MouthMemory(**{**mem.__dict__, "brief": brief}))
    assert "Talk to Sam like a friend." in with_brief and "blunt" not in with_brief and "Short and dry" not in with_brief
    plain = profile(mem)
    assert "blunt" in plain and "Short and dry" in plain


class PackRuntime(FakeRuntime):
    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
               view_hwnd: Any = None, memory_query: Any = None, pack: Any = None) -> tuple[str, int]:
        return super().submit(text, origin_hwnd=origin_hwnd, source=source, lane=lane, view_hwnd=view_hwnd,
                              memory_query=memory_query, pack=pack)


def test_the_request_pack_goes_with_a_hand_off_only_when_the_runtime_and_the_agent_take_one() -> None:
    from test_voice_v39 import Params as TellParams

    async def main(rt: Any, agent_run: Any = None) -> list:
        if agent_run is not None:
            rt.worker = SimpleNamespace(agent=SimpleNamespace(run=agent_run))
        _rt, bridge = _bridge(rt)
        bridge.pack_source = lambda: {"version": 1, "overview": {"age_s": 0.4}, "practice": None}
        task = asyncio.create_task(bridge.tell_yuki(TellParams("Restore the pages")))
        await _settle()
        task.cancel()
        return rt.submitted

    def run_with_pack(request: str, *, pack: Any = None) -> None: ...

    def run_without(request: str) -> None: ...

    got = asyncio.run(main(PackRuntime(), run_with_pack))
    assert got[-1][2]["pack"] == {"version": 1, "overview": {"age_s": 0.4}, "practice": None}
    got = asyncio.run(main(PackRuntime(), run_without))
    assert got[-1][2].get("pack") is None
    got = asyncio.run(main(FakeRuntime()))                # **kw only: never assumed to take it
    assert "pack" not in got[-1][2]


def test_the_pack_is_built_from_what_was_read_while_they_talked(tmp_path: Path) -> None:
    from yuki.voice.hands import Snapshot
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation
    from yuki.voice.practice import TalkerPractice

    snap = Snapshot(text="desk", at=time.monotonic() - 0.5, foreground=42, overview_text="[42] chrome.exe",
                    overview={"windows": [{"hwnd": 42}]}, board={"popups": []}, wall=time.time() - 0.5)
    bridge = SimpleNamespace(hands=SimpleNamespace(last=snap), has_open_voice_job=lambda: False, not_heard=None,
                             open_voice_jobs=lambda: 0, closing=False, busy_tools=0)
    conv = Conversation(settings=Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv",
                                          voice_priority_boost=False), bridge=bridge, transport=None, memory=None,
                        mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None, on_state=lambda *a: None,
                        session="t")
    conv.observer.last_user_text = "Restore the pages."
    lookup = TalkerPractice(words="Restore the pages.", turn=1, started=0.0, found=FOUND)
    conv.practice = SimpleNamespace(final_lookup=lookup)
    pack = conv._request_pack()
    assert pack["foreground"] == 42 and pack["overview"]["text"] == "[42] chrome.exe" and pack["now"] == {"popups": []}
    assert pack["practice"]["ways"][0]["id"] == 9 and pack["words"] == "Restore the pages."
    assert 0.3 < pack["overview"]["age_s"] < 5
    assert conv._apps_in_play() == []
    snap.apps = {7: "Warp.exe", 42: "chrome.exe"}
    assert conv._apps_in_play() == ["chrome.exe", "Warp.exe"]   # the one in front first
