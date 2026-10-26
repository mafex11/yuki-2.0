"""Voice v3.17 (the design notes): faster and steadier.

The memory_query hand-off (22 §4), results said by code (C1) and their voice fetched ahead (C2), the router-started
thinker (E3a), the talker's prompt by measured state (H1), the answer's length budget (H2), popups on the items line
(F1), and stability (D7: Windows QoS, items from memory's Now picture, no disk on the voice loop). Offline only:
stand-ins and localhost fakes; no microphone, no speakers, no real desktop, no real memory.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication

from test_voice_v316 import _no_real_audio_devices  # noqa: F401 - rule 7: every sounddevice stream fails the test
from test_voice_v39 import FakeRuntime, Params, _bridge, _settle
from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])


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


async def _finish(rt: FakeRuntime, job: int, text: str, *, finishing: str | None = None, ok: bool = True) -> None:
    rt.started.emit("worker", job, "x")
    if finishing is not None:
        rt.tool_called.emit("worker", job, "press", {"key": "k", "finishes_with": finishing})
        rt.tool_finished.emit("worker", job, "press", ok, "pressed k")
    rt.finished.emit("worker", job, text)
    rt.lane_done.emit("worker", job, "x", False, [])
    await _settle(20)


def _spoken(params: Params) -> list[str]:
    from pipecat.frames.frames import TTSSpeakFrame

    return [f.text for f in params.pushed if isinstance(f, TTSSpeakFrame)]


# -- 22 §4: memory is searched with the user's own words, passed explicitly --------------------------------------

def test_every_hand_off_passes_the_users_own_words_as_the_memory_query() -> None:
    from yuki.agent.memory import memory_words

    async def main() -> None:
        rt, bridge = _bridge()
        params = Params("Restore the pages", heard="restore the pages please")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        # Phase S (D10): the hand-off's words are the user's own (no talker paraphrase)
        assert rt.submitted[-1][2]["memory_query"] == memory_words("restore the pages please",
                                                                   ["restore the pages please"])
        bridge.begin_early("call-e", ["what did I do today"])
        await _settle()
        assert rt.submitted[-1][2]["memory_query"] == "what did I do today"
        await bridge.escalate(Params("x", heard="pause the video"), "media_key", {"key": "playpause"}, "no session")
        await _settle()
        assert rt.submitted[-1][2]["memory_query"] == "pause the video"
        held = Params("Open the Bro chat", heard="open the brochure")
        bridge.speak = None
        await bridge._run_held(("tell_yuki", dict(held.arguments), held.context))
        await _settle()
        assert rt.submitted[-1][2]["memory_query"].startswith("open the brochure")   # Phase S: their own words
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_a_runtime_without_the_memory_query_argument_still_gets_the_hand_off() -> None:
    class OldRuntime(FakeRuntime):
        def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
                   view_hwnd: Any = None) -> tuple[str, int]:   # no memory_query, no **kw
            return super().submit(text, origin_hwnd=origin_hwnd, source=source)

    async def main() -> FakeRuntime:
        rt, bridge = _bridge(OldRuntime())
        bridge.begin_early("c", ["open chrome"])
        await _settle()
        return rt

    assert len(asyncio.run(main()).submitted) == 1


# -- C1: a verified or short result is said by code, word for word ----------------------------------------------

def test_a_short_result_is_said_by_code_owed_and_heard_with_no_talker_run() -> None:
    from yuki.voice.narrator import OwedLedger

    async def main() -> tuple[Params, OwedLedger]:
        rt, bridge = _bridge()
        ledger = OwedLedger(stale_turns=3, log=Log())
        bridge.ledger, bridge.user_turns = ledger, lambda: 1
        params = Params("What did I do today?")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _finish(rt, rt.submitted[0][0], "Busy day: coding for an hour, then Valorant. Not much else.")
        await asyncio.wait_for(task, 1)
        return params, ledger

    params, ledger = asyncio.run(main())
    assert _spoken(params) == ["Busy day: coding for an hour, then Valorant.", "Not much else."]   # one piece each
    result, props = params.results[-1]
    assert result["said"] is True and props.run_llm is False and "how_to_handle_it" in result
    item = ledger.items[-1]
    assert item.direct and item.spoken_at is not None and item.attempts == 1   # owed, being said by code
    ledger.heard(item.spoken_at + 0.5, turn_opened=0.0, trigger="user")
    assert item.state == "heard" and item.via == "code"


@pytest.mark.parametrize("text,why", [
    ("Found three invoices. The total is 48,200. Two are overdue, from Acme.", "more than the condense rule"),
    ("I found the deposit thread. Want me to open it?", "a question"),
    ("Opened https://example.com/report for you.", "an address to describe"),
])
def test_longer_question_and_machine_results_still_go_to_the_talker(text: str, why: str) -> None:
    async def main() -> Params:
        rt, bridge = _bridge()
        params = Params("Check it")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _finish(rt, rt.submitted[0][0], text)
        await asyncio.wait_for(task, 1)
        return params

    params = asyncio.run(main())
    result, props = params.results[-1]
    assert not _spoken(params) and "said" not in result and props.run_llm is True, why


def test_a_failure_a_late_result_and_nothing_done_go_to_the_talker() -> None:
    async def run(kind: str) -> Params:
        rt, bridge = _bridge()
        turns = {"n": 1}
        bridge.user_turns = lambda: turns["n"]
        params = Params("Play the mix", acts=(kind == "nothing"))
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        if kind == "late":
            turns["n"] = 2   # they said more since
        if kind == "failed":
            rt.failed.emit("worker", job, "It broke.")
        else:
            rt.finished.emit("worker", job, "Playing the mix now.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle(20)
        await asyncio.wait_for(task, 1)
        return params

    for kind in ("failed", "late", "nothing"):
        params = asyncio.run(run(kind))
        result, props = params.results[-1]
        assert not _spoken(params) and "said" not in result, kind
    assert asyncio.run(run("late")).results[-1][0].get("late") is True
    assert asyncio.run(run("nothing")).results[-1][0].get("nothing_done") is True


def test_a_result_for_an_older_request_is_folded_unsaid_even_when_short() -> None:
    async def main() -> Params:
        rt, bridge = _bridge()
        turns = {"n": 3}
        bridge.user_turns = lambda: turns["n"]
        old = Params("Expand the accounts", call_id="old")
        t1 = asyncio.create_task(bridge.tell_yuki(old))
        await _settle()
        turns["n"] = 4
        new = Params("Add another account", call_id="new")
        t2 = asyncio.create_task(bridge.tell_yuki(new))
        await _settle()
        await _finish(rt, rt.submitted[0][0], "Accounts list is expanded.")
        await asyncio.wait_for(t1, 1)
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        return old

    old = asyncio.run(main())
    assert not _spoken(old) and "held_back" in old.results[-1][0] and old.results[-1][1].run_llm is False


def test_an_escalated_jobs_short_result_is_said_by_code_through_the_conversation() -> None:
    async def main() -> tuple[list, list]:
        rt, bridge = _bridge()
        announced: list[tuple[dict, bool]] = []
        said: list[list[str]] = []

        async def announce(payload: dict, run_llm: bool = True) -> None:
            announced.append((payload, run_llm))

        async def speak_result(lines: list[str]) -> None:
            said.append(lines)

        bridge.announce, bridge.speak_result = announce, speak_result
        await bridge.escalate(Params("x", heard="pause it"), "media_key", {"key": "playpause"}, "no session")
        await _settle()
        # (its action worked: an escalated job whose actions all failed is "nothing done", the talker's to say)
        await _finish(rt, rt.submitted[0][0], "Paused it.", finishing="Paused it.")
        return announced, said

    announced, said = asyncio.run(main())
    assert said == [["Paused it."]] and announced[-1][0]["said"] is True and announced[-1][1] is False


def test_with_no_voice_to_say_it_by_code_the_talker_says_it() -> None:
    async def main() -> Any:
        rt, bridge = _bridge()
        params = Params("x")
        del params.llm   # a bare stand-in call with nothing to push frames to
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _finish(rt, rt.submitted[0][0], "Done.")
        await asyncio.wait_for(task, 1)
        return params.results[-1]

    result, props = asyncio.run(main())
    assert "said" not in result and props.run_llm is True


def test_the_conversation_says_a_result_unguarded_one_piece_per_sentence() -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    pushed: list[str] = []
    unguarded: list[str] = []

    async def push(frame: Any, direction: Any = None) -> None:
        pushed.append(frame.text)

    conv = Conversation(settings=Settings(voice_qos=False, voice_priority_boost=False), bridge=SimpleNamespace(),
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None,
                        on_state=lambda *a: None, session="t")
    conv._llm = SimpleNamespace(push_frame=push)
    conv._tts = SimpleNamespace(unguard=unguarded.append)
    asyncio.run(conv._speak_result(["One.", "Two."]))
    assert pushed == ["One.", "Two."] and unguarded == ["One.", "Two."]
    assert conv.observer.speech_pending_since is not None   # Yuki is about to speak: nothing else starts over it


# -- C2: the result's voice fetched as the thinker writes it ------------------------------------------------------

def test_finished_sentences_of_the_done_message_are_fetched_ahead_within_the_rules() -> None:
    async def main() -> tuple[list[str], Any, Any]:
        rt, bridge = _bridge()
        fetched: list[str] = []
        bridge.prefetch_speech = lambda text: fetched.append(text) or True
        bridge.user_turns = lambda: 1
        params = Params("What did I do today?")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        for text in ("Busy day", "Busy day: coding for an hour", "Busy day: coding for an hour. Then Val",
                     "Busy day: coding for an hour. Then Valorant."):
            bridge._on_partial("worker", job, "reply", text)
        await _settle()
        first = list(fetched)
        rt.finished.emit("worker", job, "Busy day: coding for an hour. Then Valorant.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle(20)
        await asyncio.wait_for(task, 1)
        return first, params, bridge

    first, params, bridge = asyncio.run(main())
    # each finished sentence once, the last one as soon as it ends with its own mark (before the done is accepted)
    assert first == ["Busy day: coding for an hour.", "Then Valorant."]
    assert _spoken(params) == ["Busy day: coding for an hour.", "Then Valorant."]


def test_nothing_is_fetched_ahead_for_a_question_a_late_job_or_an_action_that_has_not_worked() -> None:
    async def main(case: str) -> list[str]:
        rt, bridge = _bridge()
        fetched: list[str] = []
        bridge.prefetch_speech = lambda text: fetched.append(text) or True
        turns = {"n": 1}
        bridge.user_turns = lambda: turns["n"]
        params = Params("Open it", acts=(case == "acts"))
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        if case == "late":
            turns["n"] = 2
        text = {"question": "Want me to open it? It is there."}.get(case, "It is open now. And ready.")
        bridge._on_partial("worker", job, "reply", text)
        await _settle()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return fetched

    for case in ("question", "late", "acts"):
        assert asyncio.run(main(case)) == [], case
    assert asyncio.run(main("plain")) == ["It is open now.", "And ready."]


class _PrefetchFrame:
    """Marker passed through the TTS under test: call its prefetch with these words (the bridge's role, live)."""

    def __init__(self, text: str) -> None:
        self.text = text


def test_the_voice_fetched_ahead_plays_when_said_with_one_request_and_nothing_plays_before() -> None:
    from pipecat.frames.frames import DataFrame, TTSAudioRawFrame, TTSSpeakFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import _levels
    from test_voice_v316 import _Servers, _tts

    class Prefetch(DataFrame):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.words = text

    async def main() -> None:
        async with _Servers() as fake:
            events: list = []
            tts = _tts(fake, events)
            real = tts.process_frame

            async def process_frame(frame: Any, direction: Any) -> None:
                if isinstance(frame, Prefetch):
                    assert tts.prefetch(frame.words)
                    return
                await real(frame, direction)

            tts.process_frame = process_frame
            frames = [Prefetch("Pages restored."), SleepFrame(0.6), TTSSpeakFrame("Pages restored."), SleepFrame(1.0)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert [p["text"] for p in fake.requests] == ["Pages restored."]          # one request, made ahead
            assert _levels(down) == [1000 + 37 * len("Pages restored.")]              # heard once, when said
            used = [f for e, f in events if e == "tts_prefetch" and f.get("op") == "used"]
            assert used and used[0]["first_audio_ms"] is not None and used[0]["first_audio_ms"] < 50
            assert tts.stats["prefetch_used"] == 1 and tts.stats["prefetch_wasted"] == 0
            # never said: let go after its time, with nothing played
            frames = [Prefetch("Never said."), SleepFrame(0.4)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert not [f for f in down if isinstance(f, TTSAudioRawFrame)]

    asyncio.run(main())


# -- E3a: the router-started thinker --------------------------------------------------------------------------------

class _Event:
    def __init__(self, partial: str) -> None:
        self.type = "content_block_delta"
        self.delta = SimpleNamespace(partial_json=partial)


class _Stream:
    def __init__(self, parts: list[str], gap_s: float) -> None:
        self.parts, self.gap_s = parts, gap_s

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def events() -> Any:
            for part in self.parts:
                await asyncio.sleep(self.gap_s)
                yield _Event(part)
        return events()

    async def get_final_message(self) -> Any:
        return SimpleNamespace(content=[], usage=None)


def test_the_router_returns_the_moment_its_decision_is_written() -> None:
    from yuki.voice.router import ROUTE_TOOL, decide, router_request

    parts = ['{"to', '": "thin', 'ker", "scr', 'een": tr', 'ue}', ' ']
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **req: _Stream(parts, 0.05)))
    request = router_request("click the mix", history=[("they", "open YouTube"), ("Yuki", "YouTube's up.")],
                             desk="[1] chrome.exe \"YouTube\" FOREGROUND", call=None, model="m")
    assert request["tool_choice"] == {"type": "tool", "name": "route"} and request["tools"] == [ROUTE_TOOL]
    assert "Their words now" in request["messages"][0]["content"]
    route = asyncio.run(decide(client, request))
    assert route.to == "thinker" and route.screen is True and route.ms < 400   # before the stream's last part
    talker = SimpleNamespace(messages=SimpleNamespace(stream=lambda **req: _Stream(['{"to": "talker"', ', "s'], 0.01)))
    assert asyncio.run(decide(talker, request)).to == "talker"
    broken = SimpleNamespace(messages=SimpleNamespace(stream=lambda **req: (_ for _ in ()).throw(RuntimeError("x"))))
    failed = asyncio.run(decide(broken, request))
    assert failed.to is None and failed.error == "RuntimeError"


def test_a_routed_job_is_invisible_until_the_talker_adopts_it() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        events: list[tuple[int, str]] = []
        bridge.on_job_event = lambda job_id, state, summary: events.append((job_id, state))
        assert bridge.begin_early(bridge.ROUTE_KEY, ["what did I do today"], routed=True)
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        await _settle()
        assert bridge.open_voice_jobs() == 0 and not bridge.has_open_voice_job() and bridge.work_line() is None
        assert bridge.desk_job() is None and events == []                 # the talker's view and the chat: nothing
        assert bridge.adopt_routed("call-9") and bridge.routed_job() is None
        assert bridge.open_voice_jobs() == 1 and events == [(job, "submitted"), (job, "running")]
        params = Params("What did I do today?", call_id="call-9")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        assert len(rt.submitted) == 1                                     # adopted: no second job
        await _finish(rt, job, "Busy day.")
        await asyncio.wait_for(task, 1)
        assert _spoken(params) == ["Busy day."]

    asyncio.run(main())


def test_a_routed_job_the_talker_does_not_take_is_withdrawn_unseen() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.begin_early(bridge.ROUTE_KEY, ["pause the music"], routed=True)
        await _settle()
        job_id = rt.submitted[0][0]
        rt.started.emit("worker", job_id, "x")
        await _settle()
        job = bridge.routed_job()
        bridge.settle_early({"h-media_key"})                              # the talker used a hand instead
        await _settle()
        assert rt.withdrawals == [job_id] and job.superseded and bridge.routed_job() is None
        assert bridge.desk_job() is None                                  # no hand of the talker's waits for it

    asyncio.run(main())


def test_a_tell_yuki_whose_begin_was_not_seen_still_adopts_the_routed_job() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.user_turns = lambda: 1
        bridge.begin_early(bridge.ROUTE_KEY, ["who's my accountant"], routed=True)
        await _settle()
        params = Params("Who's my accountant?", call_id="call-2")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        assert len(rt.submitted) == 1 and bridge.routed_job() is None
        await _finish(rt, rt.submitted[0][0], "That's Priya Sharma.")
        await asyncio.wait_for(task, 1)

    asyncio.run(main())


def _router_conversation(tmp_path: Path, bridge: Any, **over: Any) -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    settings = Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", voice_priority_boost=False,
                        voice_qos=False, voice_router=True, **over)
    conv = Conversation(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=MouthMemory(),
                        log=Log(), speech_end=lambda: None, on_state=lambda *a: None, session="t")
    conv._client = object()
    conv._llm = SimpleNamespace(_speculation_gate=SimpleNamespace(is_speculating=False))
    return conv


def test_the_conversation_starts_the_routed_job_and_withdraws_it_when_the_talker_answers(tmp_path: Path,
                                                                                         monkeypatch) -> None:
    import yuki.voice.router as router

    async def fake_decide(client: Any, request: dict, timeout_s: float = 3.0) -> Any:
        return router.Route(to="thinker", screen=False, ms=900.0)

    monkeypatch.setattr(router, "decide", fake_decide)

    async def main() -> tuple[Any, Any, Any]:
        rt, bridge = _bridge()
        conv = _router_conversation(tmp_path, bridge)
        context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": "what did I do today"}])
        call = SimpleNamespace(user_turn=True, entered=time.monotonic(), route=None, route_ms=None)
        conv._route_turn(context, call)
        await asyncio.sleep(0.05)
        await _settle()
        started = list(rt.submitted)
        conv._reply_done(False)                                          # the talker answered by itself
        await _settle()
        return rt, conv, started

    rt, conv, started = asyncio.run(main())
    assert len(started) == 1 and rt.withdrawals == [started[0][0]]
    assert conv.log.of("router", op="started") and conv.log.of("router", op="withdrawn", why="talker_chose_otherwise")


def test_the_router_is_on_by_default_off_when_set_and_never_runs_on_doubtful_words(tmp_path: Path) -> None:
    """vGame (5 Oct): on by default, the user asked for the thinker to start at once (it was off since v3.17)."""
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    assert Settings().voice_router is True
    rt_bridge = SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None)
    conv = Conversation(settings=Settings(voice_qos=False, voice_router=False), bridge=rt_bridge, transport=None, memory=None,
                        mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None, on_state=lambda *a: None,
                        session="t")
    conv._route_turn(SimpleNamespace(get_messages=lambda: []), SimpleNamespace(user_turn=True))
    assert getattr(conv, "_router_task", None) is None
    conv = _router_conversation(tmp_path, rt_bridge)
    conv.observer.heard, conv.observer.heard_plain = "unsure of 'brochure' (0.4)", False
    conv._route_turn(SimpleNamespace(get_messages=lambda: [{"role": "user", "content": "x"}]),
                     SimpleNamespace(user_turn=True))
    assert getattr(conv, "_router_task", None) is None



def test_sWire_r1_l3_the_conversation_sends_the_router_its_skills_line(tmp_path: Path, monkeypatch) -> None:
    """sWire review r1 L3: the pipeline's own wiring (``_route_turn`` reading ``mouth_memory.skills`` through
    ``_skill_keys``), not ``router_request`` alone: with the talker's Skills menu the router's request opens with the
    Skills line, so "play my focus playlist" stays the talker's; with no menu it has none."""
    import yuki.voice.router as router
    from yuki.skills.menu import menu_text

    seen: list[dict] = []

    async def fake_decide(client: Any, request: dict, timeout_s: float = 3.0) -> Any:
        seen.append(request)
        return router.Route(to="talker", screen=False, ms=1.0)

    monkeypatch.setattr(router, "decide", fake_decide)

    async def main(menu: str | None) -> str:
        bridge = SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None, hands=SimpleNamespace(last=None))
        conv = _router_conversation(tmp_path, bridge)
        conv.mouth_memory.skills = menu
        context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": "play my focus playlist"}])
        conv._route_turn(context, SimpleNamespace(user_turn=True, entered=time.monotonic(), route=None, route_ms=None))
        await asyncio.sleep(0.05)
        assert seen, "the router was never asked"
        return seen.pop()["messages"][0]["content"]

    menu = menu_text(None, audience="talker")
    assert menu
    content = asyncio.run(main(menu))
    assert content.startswith("[Skills line") and content.endswith('Their words now: "play my focus playlist"')
    assert "Skills line" not in asyncio.run(main(None))

# -- H1: the talker's prompt by measured state ----------------------------------------------------------------------

def test_the_prompt_is_layered_by_state_with_breakpoints_before_the_call_layer() -> None:
    from yuki.voice.mouth import HANDS_PROMPT, PEOPLE_LABEL, MouthMemory, mouth_system_blocks

    memory = MouthMemory(source="memory", standing="- short", personality="Banters.", people="Rohan: WhatsApp")
    plain = mouth_system_blocks(memory, tail=["t"], channel="marker")
    assert [b["text"] for b in plain][1] == HANDS_PROMPT and "cache_control" in plain[1]   # the first breakpoint
    assert "cache_control" in plain[-1] and sum("cache_control" in b for b in plain) == 2
    call = mouth_system_blocks(memory, tail=["t"], channel="marker", call="Discord")
    assert call[:2] == plain[:2] and call[2]["text"].startswith("[Private note")          # after the breakpoint
    handless = mouth_system_blocks(memory, tail=["t"], channel="marker", hands=False)
    assert all(b["text"] != HANDS_PROMPT for b in handless) and "cache_control" in handless[0]
    assert PEOPLE_LABEL in plain[2]["text"] and "Rohan: WhatsApp" in plain[2]["text"]


def test_the_cached_prompt_stays_under_its_measured_size() -> None:
    """Measured 15.3k cached tokens (no call) / 15.9k (in a call) with a real profile (was 17.7k / 18.4k): a guard on
    the characters of the static layers so the prompt does not creep back past the ~18k step unnoticed.

    vfix (3 Oct, V1-V12): with today's larger profile the talker measured 17.1k cached tokens (no call) / 17.7k (in a
    call) against main's 16.6k / 17.2k, and the time to first token was the same as main's in a call (p50 1,035 vs
    1,034 ms, 8 interleaved rounds, scripts/voice_ttft.py). A first draft at 18.4k in a call cost +300 ms: the step is
    between the two, so this guard stays tight."""
    from yuki.voice.mouth import CALL_LAYER, HANDS_PROMPT, MOUTH_PROMPT, SPEECH_CHANNEL_PROMPT, VOICE_MARKS_PROMPT

    # vfix4: the voice marks are their own section (sent only with a voice that performs them): still counted here,
    # so the guard holds for the largest core a conversation can send
    assert len(MOUTH_PROMPT) + len(SPEECH_CHANNEL_PROMPT) + len(HANDS_PROMPT) + len(VOICE_MARKS_PROMPT) <= 41_800
    assert len(CALL_LAYER) <= 2_300


def test_each_voice_layout_with_the_quick_memory_tool_stays_at_mains_size() -> None:
    """vfix4 review F6: one guard per voice layout, so neither sits past the ~18k step unnoticed. Measured 4 Oct with
    ``scripts/voice_ttft.py --tokens-only`` (count of the whole request; an empty profile, so a real one adds the same
    to each): main's talker 16,796 tokens; this tree with the quick memory tool, a Gemini conversation (the marks
    section in the core) 16,784, an Aura or ElevenLabs one (no marks) 16,446. Main itself sits just under the step
    with today's profile (17,662), so neither layout may grow past these characters without measuring again."""
    import json

    from yuki.voice.mouth import HANDS_PROMPT, MOUTH_PROMPT, SPEECH_CHANNEL_PROMPT, VOICE_MARKS_PROMPT
    from yuki.voice.recall import QUICK_MEMORY

    core = len(MOUTH_PROMPT) + len(SPEECH_CHANNEL_PROMPT) + len(HANDS_PROMPT)
    tool = len(json.dumps(QUICK_MEMORY))
    assert core + len(VOICE_MARKS_PROMPT) + tool <= 41_650   # Gemini (Leda performs the marks)
    assert core + tool <= 40_600                              # Deepgram Aura, ElevenLabs (they strip the marks)


def test_state_guidance_rides_only_on_the_turns_in_that_state() -> None:
    from yuki.voice.mouth import (
        HAND_TROUBLE_NOTE, RECOGNITION_GUIDE, REOPEN_GUIDE, WORK_GUIDE, MOUTH_PROMPT, volatile_block,
    )

    plain = volatile_block()["text"]
    for guide in (RECOGNITION_GUIDE, WORK_GUIDE, HAND_TROUBLE_NOTE, REOPEN_GUIDE):
        assert guide.strip()[:60] not in plain and guide.strip()[:60] not in MOUTH_PROMPT
    assert RECOGNITION_GUIDE.strip()[:60] in volatile_block(heard="unsure of 'harsh' (0.40)")["text"]
    assert WORK_GUIDE.strip()[:60] in volatile_block(work='task 4 "x" running for 3 s')["text"]
    assert HAND_TROUBLE_NOTE.strip()[:60] in volatile_block(state={"hand_trouble": True})["text"]
    assert REOPEN_GUIDE.strip()[:60] in volatile_block(state={"closed": True})["text"]
    assert "Long rest of it" in volatile_block(state={"held": "Long rest of it."})["text"]


def test_the_people_directory_is_probed_capped_and_skipped_when_absent() -> None:
    from yuki.voice.mouth import PEOPLE_CHARS, fetch_people

    class Access:
        def __init__(self, client: Any) -> None:
            self.client = client

        def call(self, what: str, fn: Any, *, timeout_s: float) -> Any:
            return fn(self.client)

    assert fetch_people(Access(SimpleNamespace())) is None                       # an older memory: skipped
    assert fetch_people(SimpleNamespace()) is None
    lines = [f"Person {i}: WhatsApp, Discord" for i in range(200)]
    got = fetch_people(Access(SimpleNamespace(people_directory=lambda: "\n".join(lines))))
    assert got and len(got) <= PEOPLE_CHARS and got.splitlines()[0] == lines[0]
    assert fetch_people(Access(SimpleNamespace(people_directory=lambda: ["A: x", "B: y"]))) == "A: x\nB: y"
    assert fetch_people(Access(SimpleNamespace(people_directory=lambda: (_ for _ in ()).throw(OSError())))) is None


def test_the_conversation_measures_the_states_from_the_hands_and_the_bridge(tmp_path: Path) -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    hands = SimpleNamespace(halted=None, trouble_at=None, closed_at=None)
    bridge = SimpleNamespace(hands=hands, closed_at=None)
    conv = Conversation(settings=Settings(voice_qos=False), bridge=bridge, transport=None, memory=None,
                        mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None, on_state=lambda *a: None,
                        session="t")
    assert conv._prompt_state() == {}
    hands.trouble_at = time.monotonic()
    bridge.closed_at = time.monotonic()
    state = conv._prompt_state()
    assert state.get("hand_trouble") and state.get("closed")
    hands.trouble_at = time.monotonic() - conv.HAND_TROUBLE_S - 1
    hands.halted = "scroll: did not move"
    assert conv._prompt_state().get("hand_trouble")


# -- H2: the answer's length, by structure -----------------------------------------------------------------------

def _channel(text: str, cap: int = 0, parts: int = 4) -> tuple[str, Any]:
    from yuki.voice.mouth import ReplyChannel

    channel = ReplyChannel(cap_words=cap)
    step = max(1, len(text) // parts)
    out = "".join(channel.feed(text[i:i + step]) for i in range(0, len(text), step))
    out += channel.finish()
    return out, channel


def test_a_more_line_holds_the_rest_and_the_word_cap_ends_speech_at_a_sentence() -> None:
    out, channel = _channel("say\nThree things, mainly coding. Want the rest?\nmore\nThen Valorant, then a call.")
    assert out.strip() == "Three things, mainly coding. Want the rest?" and channel.held_by == "more"
    assert channel.held == "Then Valorant, then a call." and channel.facts()["held_chars"] == len(channel.held)
    words = " ".join(f"word{i}" for i in range(30))
    out, channel = _channel(f"say\n{words}. And then this. And that.", cap=25, parts=9)
    assert out.endswith(f"{words}.") and channel.held_by == "cap" and channel.held == "And then this. And that."
    out, channel = _channel("say\nMore than you think, honestly.", cap=25)
    assert out == "More than you think, honestly." and channel.held_by is None   # a word, not the divider
    out, channel = _channel("say\nSure.\nmore", parts=1)
    assert out.strip() == "Sure." and channel.held_by == "more" and channel.held == ""
    out, _channel_ = _channel("say\nMo", parts=1)
    assert out == "Mo"                                                         # a held line start is never lost


def test_more_of_result_gives_the_talkers_own_held_words_when_no_result_has_more(tmp_path: Path) -> None:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    conv = Conversation(settings=Settings(voice_qos=False), bridge=SimpleNamespace(), transport=None, memory=None,
                        mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None, on_state=lambda *a: None,
                        session="t")
    conv._marker_report(SimpleNamespace(user_turn=True),
                        SimpleNamespace(facts=lambda: {"marker": "say"}, held="One more. Two more. Three more."))
    assert conv._prompt_state().get("held") == "One more. Two more. Three more."

    async def main() -> Params:
        rt, bridge = _bridge()
        bridge.more_held = conv.more_held
        params = Params("x")
        await bridge.more_of_result(params)
        return params

    params = asyncio.run(main())
    result = params.results[-1][0]
    assert result["more"] == "One more. Two more." and result["more_available"] is True
    assert conv.more_held() == ("Three more.", False) and conv.more_held() is None


# -- F1 and D7: popups on the items line, items from memory's Now picture -----------------------------------------

def _desk() -> Any:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import voice_scenarios as vs

    return vs._youtube_desk(vs.Timeline())


def test_a_stand_in_desk_never_reaches_the_real_screens_now_picture(monkeypatch) -> None:
    import yuki.memory.now as now
    from yuki.voice.hands import Hands, now_source_for

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real Now read")))
    desk = _desk()
    assert now_source_for(desk) is None
    hands = Hands(desk)
    hands.snapshot()
    hands._read_items(desk.CHROME, desk.title(desk.CHROME))   # read on the stand-in, never memory's service


def test_a_popup_from_the_now_picture_is_on_the_items_line_and_the_click_hand_reaches_it() -> None:
    from yuki.voice.hands import Hands

    desk = _desk()
    POPUP = 0x7777
    clicked: list[tuple[int, int]] = []
    tree_of = desk.get_window_tree

    def get_window_tree(hwnd: int, **kw: Any) -> dict[str, Any]:
        if int(hwnd) != POPUP:
            return tree_of(hwnd, **kw)
        return {"hwnd": POPUP, "title": "", "process_name": "chrome.exe", "status": "ok", "truncated": False,
                "elements": [{"id": 0, "role": "Pane", "name": "Restore pages?", "depth": 0, "bounds": (0, 0, 300, 120),
                              "center": (150, 60)},
                             {"id": 1, "role": "Button", "name": "Restore", "depth": 1, "parent": 0,
                              "bounds": (200, 80, 280, 110), "center": (240, 95), "is_interactive": True}],
                "captured_at": time.time(), "elapsed_ms": 4.0, "note": ""}

    real_click = desk.click

    def click(x: int, y: int, **kw: Any) -> Any:
        clicked.append((int(x), int(y)))
        result = real_click(x, y, **kw)
        result.details = {**(getattr(result, "details", None) or {}), "foreground_title_changed": True,
                          "page_changed": True}
        return result

    overview_of = desk.get_desktop_overview

    def get_desktop_overview() -> dict[str, Any]:
        # the stand-in's window list as it is, with the bubble (no window of the list) in front when it has the keyboard
        front, desk.front = desk.front, desk.CHROME
        try:
            overview = overview_of()
        finally:
            desk.front = front
        if front == POPUP:
            overview["foreground_hwnd"] = POPUP
        return overview

    desk.get_window_tree, desk.click, desk.get_desktop_overview = get_window_tree, click, get_desktop_overview
    board = {"windows": [], "popups": [{"hwnd": POPUP, "app": "Google Chrome", "title": "", "age_s": 2.0,
                                       "text": "Chrome didn't shut down correctly.", "controls": ["Restore", "Close"]}]}
    hands = Hands(desk)
    hands.now_source = lambda: board
    snap = hands.snapshot()
    assert f"popup [{POPUP}] Google Chrome" in snap.text and '1 "Restore"' in snap.text and POPUP in snap.hwnds
    desk.front = POPUP   # the bubble has the keyboard (as Chrome's restore bubble does)
    hands._read_items(POPUP, "")
    front = hands.snapshot()
    # in front, its own line says it is a popup of that app and which hwnd a click takes (a replay clicked Chrome's)
    assert f"popup [{POPUP}] Google Chrome in front" in front.text and f"click it with window {POPUP}" in front.text
    ok, report, facts = hands._run("click", {"window": POPUP, "item": 1, "say_when_done": ""})
    assert clicked == [(240, 95)] and "Restore" in report


def test_items_come_from_the_now_picture_when_its_entry_is_fresh_and_whole() -> None:
    from yuki.voice.hands import NOW_CONTROLS_CAP, Hands

    desk = _desk()
    hands = Hands(desk)
    hands.snapshot()
    title = desk.title(desk.CHROME)
    reads: list[str] = []
    real = hands._dispatcher.dispatch
    hands._dispatcher.dispatch = lambda name, args: (reads.append(name), real(name, args))[1]
    entry = {"hwnd": desk.CHROME, "read_at": 1.0, "stale": False, "controls": ["Home", "Shorts", "Mix"], "messages": []}
    hands.now_source = lambda: {"windows": [entry], "popups": []}
    hands._read_items(desk.CHROME, title)
    assert reads == [] and [i["name"] for i in hands.items.items] == ["Home", "Shorts", "Mix"]   # no read here
    entry["controls"] = [f"Control {i}" for i in range(NOW_CONTROLS_CAP)]                       # at the cap
    hands._read_items(desk.CHROME, title)
    assert reads == ["look_at_window"] and hands.items.read                                    # the page's own read
    reads.clear()
    entry.update(stale=True, controls=["Home"])
    hands._read_items(desk.CHROME, title)
    assert reads == ["look_at_window"]


def test_other_visible_windows_are_listed_only_when_mostly_seen_and_freshly_read() -> None:
    from yuki.voice.hands import Hands

    desk = _desk()
    hands = Hands(desk)
    windows = [{"hwnd": 501, "app": "Discord", "title": "#general", "share": 0.6, "read_at": 1.0, "stale": False,
                "controls": ["General", "Voice"]},
               {"hwnd": 502, "app": "Notepad", "title": "x", "share": 0.1, "read_at": 1.0, "controls": ["File"]},
               {"hwnd": 503, "app": "Slack", "title": "y", "share": 0.9, "read_at": 1.0, "stale": True,
                "controls": ["Send"]}]
    hands.now_source = lambda: {"windows": windows, "popups": []}
    snap = hands.snapshot()
    assert "items in [501] Discord" in snap.text and "[502]" not in snap.text and "[503]" not in snap.text
    assert 501 in snap.hwnds and 502 not in snap.hwnds


# -- D7: Windows QoS ---------------------------------------------------------------------------------------------

class _FakeWin:
    """kernel32 and avrt stand-ins that record what was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def k32(self) -> Any:
        win = self

        class K:
            @staticmethod
            def GetCurrentProcess() -> int:
                return 1

            @staticmethod
            def GetCurrentThread() -> int:
                return 2

            @staticmethod
            def SetProcessInformation(handle: int, cls: int, info: Any, size: int) -> int:
                state = info._obj
                win.calls.append(("process", (cls, state.ControlMask, state.StateMask)))
                return 1

            @staticmethod
            def SetThreadInformation(handle: int, cls: int, info: Any, size: int) -> int:
                state = info._obj
                win.calls.append(("thread", (cls, state.ControlMask, state.StateMask)))
                return 1

        return K()

    def avrt(self) -> Any:
        win = self

        class A:
            @staticmethod
            def AvSetMmThreadCharacteristicsW(task: str, index: Any) -> int:
                win.calls.append(("mmcss", task))
                return 99

            @staticmethod
            def AvRevertMmThreadCharacteristics(handle: int) -> int:
                win.calls.append(("revert", handle))
                return 1

        return A()


def test_the_process_and_the_voice_threads_are_put_on_time_and_handed_back(monkeypatch) -> None:
    from yuki.voice import qos

    win = _FakeWin()
    monkeypatch.setattr(qos, "_kernel32", win.k32)
    monkeypatch.setattr(qos, "_avrt", win.avrt)
    monkeypatch.setattr(qos, "_holders", 0)
    assert qos.process_high_qos()["ok"] and qos.process_high_qos().get("already")
    assert qos.process_default_qos().get("held") == 1 and qos.process_default_qos()["ok"]
    assert win.calls == [("process", (qos.PROCESS_POWER_THROTTLING, qos.EXECUTION_SPEED | qos.IGNORE_TIMER_RESOLUTION,
                                      0)), ("process", (qos.PROCESS_POWER_THROTTLING, 0, 0))]
    win.calls.clear()
    handle = qos.ThreadQos()
    facts = handle.apply()
    assert facts["mmcss"] == "Audio" and facts["throttling"] == "off"
    assert handle.revert()["mmcss"] == "left"
    assert win.calls == [("thread", (qos.THREAD_POWER_THROTTLING, qos.EXECUTION_SPEED, 0)), ("mmcss", "Audio"),
                         ("revert", 99), ("thread", (qos.THREAD_POWER_THROTTLING, 0, 0))]
    win.calls.clear()
    monkeypatch.setattr(qos, "_applied", {})
    assert qos.ensure_thread() is not None and qos.ensure_thread() is None   # once per thread
    assert [c[0] for c in win.calls] == ["thread", "mmcss"]


def test_a_live_conversation_boosts_its_loop_thread_and_restores_it(tmp_path: Path, monkeypatch) -> None:
    from yuki.voice import qos
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    win = _FakeWin()
    monkeypatch.setattr(qos, "_kernel32", win.k32)
    monkeypatch.setattr(qos, "_avrt", win.avrt)
    monkeypatch.setattr(qos, "_holders", 0)
    log = Log()
    conv = Conversation(settings=Settings(voice_priority_boost=False, voice_qos=True), bridge=SimpleNamespace(),
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=log, speech_end=lambda: None,
                        on_state=lambda *a: None, session="t")
    conv._boost()
    conv._unboost()
    kinds = [c[0] for c in win.calls]
    assert kinds == ["process", "thread", "mmcss", "revert", "thread", "process"]
    assert log.of("qos", op="loop") and log.of("qos", op="loop_end")
    win.calls.clear()
    conv = Conversation(settings=Settings(voice_priority_boost=False, voice_qos=False), bridge=SimpleNamespace(),
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None,
                        on_state=lambda *a: None, session="t")
    conv._boost()
    conv._unboost()
    assert win.calls == []


def test_the_speakers_writer_thread_is_put_on_time_and_handed_back(monkeypatch) -> None:
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice import qos
    from yuki.voice.transport import SpeakerOutput

    win = _FakeWin()
    monkeypatch.setattr(qos, "_kernel32", win.k32)
    monkeypatch.setattr(qos, "_avrt", win.avrt)
    out = SpeakerOutput(SimpleNamespace(write=lambda pcm: None), TransportParams(audio_out_enabled=True))
    out._executor.submit(lambda: None).result(timeout=2)
    assert out.qos_facts and out.qos_facts.get("mmcss") == "Audio"
    asyncio.run(out.cleanup())
    out._executor.shutdown(wait=True)
    assert ("revert", 99) in win.calls


def test_the_talkers_stream_holds_back_after_the_more_line_and_past_the_cap() -> None:
    """H2 in the pipeline: what follows the more line (or the cap's sentence end) never reaches the voice or the
    conversation; a line start held as a possible divider is still said when the reply ends on it."""
    from pipecat.services.anthropic.llm import AnthropicLLMService

    from test_voice_v316 import _events
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import LlmCall, _layered_service

    reports: list[Any] = []

    class Hooks:
        cap = 0

        def channel(self) -> str:
            return "marker"

        def reply_cap(self) -> int:
            return self.cap

        def marker_report(self, call: Any, channel: Any) -> None:
            reports.append(channel)

        def fallbacks(self) -> list:
            return []

        def sticky_index(self, size: int) -> int:
            return 0

        def model_ok(self, *a: Any) -> None:
            pass

        def acked(self) -> None:
            pass

    hooks = Hooks()

    async def run(parts: list[str]) -> str:
        llm = _layered_service(AnthropicLLMService, MouthMemory(), hooks)(
            api_key="x", settings=AnthropicLLMService.Settings(model="m"))
        events = _events(parts)

        async def fake_open(api_call: Any, params: Any) -> tuple[Any, Any]:
            async def gen() -> Any:
                for e in events[1:]:
                    yield e
            return gen(), events[0]

        llm._yuki_open = fake_open
        llm._yuki_call = LlmCall(entered=time.monotonic(), user_turn=True)
        stream = await llm._create_message_stream(None, {})
        return "".join([e.delta.text async for e in stream if getattr(e, "type", "") == "content_block_delta"
                        and hasattr(e.delta, "text")])

    said = asyncio.run(run(["say\nMostly coding. Want ", "the rest?\nmo", "re\nThen Valorant, then a call."]))
    assert said.strip() == "Mostly coding. Want the rest?" and reports[-1].held == "Then Valorant, then a call."
    hooks.cap = 6
    said = asyncio.run(run(["say\nOne two three four five six. Seven ", "eight. Nine."]))
    assert said.strip() == "One two three four five six." and reports[-1].held_by == "cap"
    hooks.cap = 0
    said = asyncio.run(run(["say\nSure.\nM"]))
    assert said == "Sure.\nM"                                           # the held line start, said at the end


def test_the_lookup_example_has_no_concrete_answer_the_talker_could_copy() -> None:
    """Found while measuring v3.17: mid-conversation, "who's my accountant?" was answered "That's Priya Sharma." (the
    name in the worked example) with no lookup line at all, 3-6 of 8 with the v3.16 prompt and 8 of 8 with the trimmed
    one (the real talker, the speed1003 history). The example now says it with placeholders: 8 of 8 handed over."""
    from yuki.voice.mouth import MOUTH_PROMPT

    assert "Priya Sharma" not in MOUTH_PROMPT and "<name>" in MOUTH_PROMPT and "never from these examples" in MOUTH_PROMPT
