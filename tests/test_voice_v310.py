"""Voice v3.10 (2026-09-28, the 09:06-09:11 session: "it felt like talking with 2 people at the same time").

Covered offline: the spoken-line check (reasoning, talk about the user, internals and repeats never heard; short lines
and kept lines not delayed; fails open), the Gemini voice holding a piece's audio for its verdict, one job per
request (a second hand-off in the same user turn merges, a second identical hand is not run), a stale hand refusal
passed to the thinker by code, the held decision made once per reply, names on the screen (for recognition, the
Speech line and the talker), the call's evidence (a sure call keeps stay_quiet, stale evidence refuses it once for
plain words), and "go quiet for two hours" kept across restarts and undone from the tray.
No microphone, no speakers, no network: fakes and stand-ins only.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QObject

from test_voice_v39 import FakeRuntime, Params, _bridge, _hand_params, _hands, _run_llm, _settle
from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])


# -- A. the spoken-line check ---------------------------------------------------------------------------

def _guard(answers: dict[str, str] | None = None, delay: float = 0.0, fail: bool = False) -> Any:
    from yuki.voice.speechguard import SpeechGuard

    seen: list[str] = []

    async def judge(text: str, said: list[str], user: str) -> str:
        seen.append(text)
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("down")
        return (answers or {}).get(text, "SAY")

    guard = SpeechGuard(None, "haiku", judge=judge, timeout_s=0.5)
    guard.seen = seen
    return guard


def test_guard_drops_reasoning_and_keeps_speech() -> None:
    async def main() -> None:
        leak = "They're on a voice call on Zen. Those words were for me, so I'll switch tabs."
        guard = _guard({leak: "DROP: about_user"})
        drops: list[int] = []
        guard.on_drop = lambda: drops.append(1)
        assert (await guard.decide(guard.start(leak), leak)).ok is False
        ok = "Sure, opening the second YouTube tab."
        assert (await guard.decide(guard.start(ok), ok)).ok is True
        assert drops == [1] and guard.stats["caught"] == 1

    asyncio.run(main())


def test_guard_lets_short_lines_through_without_a_model_call_and_catches_exact_repeats() -> None:
    async def main() -> None:
        guard = _guard()
        assert (await guard.decide(guard.start("Opening YouTube."), "Opening YouTube.")).ok
        assert (await guard.decide(guard.start("YouTube's up."), "YouTube's up.")).ok
        assert guard.seen == []  # short lines: no model call, no wait
        again = guard.start("YouTube's up.")   # the same line again, same turn: "YouTube's up. YouTube's up."
        assert (await guard.decide(again, "YouTube's up.")).ok is False
        guard.new_turn()   # the user spoke ("again"): "Done." is a new answer
        assert (await guard.decide(guard.start("Done."), "Done.")).ok is True
        guard.new_turn()
        assert (await guard.decide(guard.start("Done."), "Done.")).ok is True

    asyncio.run(main())


def test_guard_catches_two_copies_of_one_line_written_back_to_back() -> None:
    async def main() -> None:
        guard = _guard(delay=0.05)
        line = "Opening YouTube in a new tab now."
        first, second = guard.start(line), guard.start(line)
        assert (await guard.decide(first, line)).ok and not (await guard.decide(second, line)).ok

    asyncio.run(main())


def test_guard_fails_open_on_error_and_on_timeout() -> None:
    async def main() -> None:
        broken = _guard(fail=True)
        line = "Let me find that chat for you now."
        assert (await broken.decide(broken.start(line), line)).ok and broken.stats["errors"] == 1
        slow = _guard({line: "DROP: reasoning"}, delay=1.0)
        verdict = await slow.decide(slow.start(line), line, wait_s=0.1)
        assert verdict.ok and verdict.by == "timeout" and slow.stats["timeouts"] == 1

    asyncio.run(main())


def test_verdict_parsing_is_a_fixed_format() -> None:
    from yuki.voice.speechguard import parse_verdict

    assert parse_verdict("SAY") == (True, "")
    assert parse_verdict("DROP: internals") == (False, "internals")
    assert parse_verdict("drop:repeat.") == (False, "repeat")
    assert parse_verdict("") == (True, "")   # unreadable: said (fails open)


def _judge_drop_leak(seen: list[str]) -> Any:
    async def judge(text: str, said: list[str], user: str) -> str:   # a stand-in for the model (test code)
        seen.append(text)
        await asyncio.sleep(0.2)
        return "DROP: reasoning" if text in ("Those words were for me,", "so I'll open it.") else "SAY"
    return judge


def test_gemini_holds_a_pieces_audio_for_its_verdict_and_drops_its_words_too() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service

    async def main() -> None:
        async with FakeServers() as fake:
            leak = "Those words were for me, so I'll open it."
            tts = _service(fake)
            guard = _guard(delay=0.2)
            # the voice says the first clause on its own (v3.7): both halves of the leak are checked, each dropped
            guard.judge = _judge_drop_leak(guard.seen)
            tts.set_guard(guard)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame(leak + " "), LLMTextFrame("Opening WhatsApp now, one sec."),
                      LLMFullResponseEndFrame(), SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            words = [f.text for f in down if isinstance(f, TTSTextFrame)]
            assert not any("words were for me" in w for w in words)  # never in the conversation's record
            assert any("Opening WhatsApp" in w for w in words)
            assert _levels(down) == [1000 + 37 * len("Opening WhatsApp now, one sec.")]  # only the kept piece heard
            assert tts.stats["guarded"] == 2

    asyncio.run(main())


def test_a_reply_whose_every_piece_was_dropped_still_ends_cleanly() -> None:
    """Offline 2026-09-28: a reply whose only line was dropped ended "with no audio" (an error frame) and left the
    narrator waiting for a reply still owed. It closes with a breath of silence now."""
    from pipecat.frames.frames import (
        ErrorFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSStoppedFrame, TTSTextFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service

    async def main() -> None:
        async with FakeServers() as fake:
            leak = "They're on a voice call on Zen right now."
            tts = _service(fake)
            tts.set_guard(_guard({leak: "DROP: about_user"}, delay=0.1))
            frames = [LLMFullResponseStartFrame(), LLMTextFrame(leak), LLMFullResponseEndFrame(), SleepFrame(1.2)]
            down, up = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert _levels(down) == []   # nothing audible
            assert not [f for f in down if isinstance(f, TTSTextFrame)]
            assert sum(isinstance(f, TTSStoppedFrame) for f in down) == 1
            assert not [f for f in list(down) + list(up) if isinstance(f, ErrorFrame)]

    asyncio.run(main())


# -- B. one job, one step, one outcome ----------------------------------------------------------------

def test_a_second_hand_off_in_the_same_user_turn_is_the_same_request() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.user_turns = lambda: 3
        first = Params("Open YouTube", call_id="c1")
        t1 = asyncio.create_task(bridge.tell_yuki(first))
        await _settle()
        rt.started.emit("worker", rt.submitted[0][0], "x")
        await _settle()
        second = Params("Open YouTube now", call_id="c2")
        t2 = asyncio.create_task(bridge.tell_yuki(second))
        await _settle()
        assert len(rt.submitted) == 1  # one job
        assert rt.steers and rt.steers[0][0] == rt.submitted[0][0]  # the second call went to it
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)

    asyncio.run(main())


class _OpenDesk:
    """A stub backend for open_url: slow enough that a second call arrives while the first runs."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.scrolled = 0

    def open_url(self, url: str, **_: Any) -> Any:
        self.opened.append(url)
        time.sleep(0.3)
        return SimpleNamespace(ok=True, summary=f"opened {url}", details={"hwnd": 7}, elapsed_ms=300.0)

    def activity_facts(self) -> dict:
        return {"media": []}

    def get_desktop_overview(self) -> dict:
        return {"windows": [], "foreground_hwnd": None}

    def format_overview(self, overview: dict) -> str:
        return ""


def test_the_same_opening_twice_runs_once_with_one_done_line() -> None:
    async def main() -> None:
        from pipecat.frames.frames import TTSSpeakFrame

        rt, bridge = _bridge()
        desk = _OpenDesk()
        hands = _hands(desk, bridge)
        args = {"say_first": "Opening YouTube.", "url": "https://www.youtube.com", "say_when_done": "YouTube's up."}
        a = _hand_params("open_url", dict(args))
        b = _hand_params("open_url", dict(args, say_first="Opening YouTube now.", url="https://www.youtube.com/"))
        b.tool_call_id = "h-open-2"
        await asyncio.gather(hands.handle(a), hands.handle(b))
        assert desk.opened == ["https://www.youtube.com"]   # run once
        assert b.results[-1][0].get("already") and _run_llm(b.results[-1][1]) is False
        c = _hand_params("open_url", dict(args))   # asked again right after it was verified: still the same step
        c.tool_call_id = "h-open-3"
        await hands.handle(c)
        assert desk.opened == ["https://www.youtube.com"]
        done_lines = [f.text for p in (a, b, c) for f in p.pushed if isinstance(f, TTSSpeakFrame)]
        assert done_lines.count("YouTube's up.") == 1

    asyncio.run(main())


def test_a_refused_hand_on_a_stale_tab_goes_to_the_thinker_by_code() -> None:
    async def main() -> None:
        from yuki.voice.hands import Snapshot

        rt, bridge = _bridge()
        announced: list = []

        async def announce(payload: dict, run_llm: bool = True) -> None:
            announced.append(payload)

        bridge.announce = announce
        desk = _OpenDesk()
        desk.tab = lambda *a, **k: None
        hands = _hands(desk, bridge)
        hands.last = Snapshot(text="x", hwnds={42}, tabs={42: ["YouTube"]}, tab_ids={42: ["id-1"]})
        params = _hand_params("tab", {"window": 42, "action": "close", "tabs": [9], "named": "the second YouTube tab"})
        await hands.handle(params)
        result, props = params.results[-1]
        assert result.get("passed_on") is True and _run_llm(props) is False   # no talker run narrates the refusal
        await _settle()
        assert len(rt.submitted) == 1 and rt.submitted[0][2].get("lane") == "worker"
        assert "tabs line you were shown" in rt.submitted[0][1]   # the thinker gets the report and reads afresh

    asyncio.run(main())


def test_a_calls_hold_is_decided_once_when_the_reply_ends() -> None:
    """Live 09:10:01 the reply's words were dropped because its hand-off would be held, then the same hand-off ran a
    moment later (the turn's doubt had moved on): the user heard a question and an outcome. Decided once now."""
    from yuki.voice.pipeline import Conversation

    conv = Conversation.__new__(Conversation)
    conv._llm = SimpleNamespace(_yuki_hold={"t1": True, "t2": False}, _yuki_call=None)
    conv.observer = SimpleNamespace(heard=None)
    assert conv._held("tell_yuki", "t1") is True     # decided held: held, whatever the doubt is now
    conv.observer = SimpleNamespace(heard="unsure of 'x'")
    assert conv._held("tell_yuki", "t2") is False    # decided not held: runs


# -- C. names on the screen -----------------------------------------------------------------------------

def test_names_on_the_screen_come_from_the_tree_structure() -> None:
    from yuki.voice.screennames import names_from_tree

    tree = {"elements": [
        {"id": 0, "role": "Window", "name": "WhatsApp", "depth": 0},
        {"id": 1, "role": "Button", "name": "New chat", "depth": 2},
        {"id": 2, "role": "DataItem", "name": "Bro Yesterday on my way Starred chat", "depth": 3},
        {"id": 3, "role": "Text", "name": "Bro", "depth": 4},
        {"id": 4, "role": "Text", "name": "Yesterday", "depth": 4},
        {"id": 5, "role": "DataItem", "name": "Res 09:02 ok see you tomorrow", "depth": 3},
        {"id": 6, "role": "Text", "name": "09:02", "depth": 4},
        {"id": 7, "role": "Text", "name": "Res", "depth": 4},
        {"id": 8, "role": "DataItem", "name": "a very long row with no short text inside it at all", "depth": 3},
        {"id": 9, "role": "DataItem", "name": "Mother", "depth": 3},
    ]}
    assert names_from_tree(tree) == ["Bro", "Res", "Mother", "New chat"]   # list items first; no guess for id 8


def test_the_speech_line_names_screen_names_and_a_name_run_into_the_next_word() -> None:
    from yuki.voice.pipeline import heard_note

    words = [{"word": "Open", "confidence": 0.99}, {"word": "the", "confidence": 0.99},
             {"word": "brochure.", "confidence": 0.55}]
    note, _low, near = heard_note(words, ["Bro", "Elina"], screen=["Bro", "Res"])
    assert "'brochure' may be Bro said together with the next word" in note and near >= 1
    clear = [{"word": "Scroll", "confidence": 0.99}, {"word": "down.", "confidence": 0.99}]
    assert heard_note(clear, ["Bro"], screen=["Bro", "Res", "Search", "Settings"])[0] is None


def test_the_talkers_desktop_block_lists_the_names_in_front() -> None:
    from yuki.voice.hands import Hands

    class Desk(_OpenDesk):
        def get_window_tree(self, hwnd: int, **_: Any) -> dict:
            return {"elements": [{"id": 1, "role": "ListItem", "name": "Res", "depth": 3},
                                 {"id": 2, "role": "ListItem", "name": "Bro", "depth": 3}]}

    hands = Hands(Desk())
    windows = [{"hwnd": 9, "process_name": "WhatsApp.Root.exe", "title": "WhatsApp", "bounds": (0, 0, 10, 10)}]
    hands._dispatcher = SimpleNamespace(activity_facts=lambda: {"media": []},
                                        overview_text=lambda: ("windows", {"windows": windows, "foreground_hwnd": 9}, 0.0))
    got: list = []
    hands.refresh_screen_names(got.append)
    deadline = time.monotonic() + 2
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [["Res", "Bro"]]
    assert "names on the screen in WhatsApp.Root" in hands.snapshot().text


# -- D. calls: evidence decides how sure the talker is -----------------------------------------------------

def _quiet_params(bridge: Any) -> Any:
    p = Params()
    p.function_name = "stay_quiet"
    p.arguments = {}
    return p


def test_a_sure_call_keeps_the_talkers_silence_and_stale_evidence_refuses_it_once() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        bridge.user_turn = lambda: True
        bridge.spoke_in_reply = lambda: False
        bridge.in_exchange = lambda: True
        bridge.user_turns = lambda: 5
        bridge.call_mode = lambda: True
        bridge.call_evidence = lambda: "fresh"
        sure = _quiet_params(bridge)
        await bridge.stay_quiet(sure)
        assert sure.results[-1][0] == {"ok": True}   # on a real call, silence stands
        bridge.call_evidence = lambda: "stale"
        bridge.user_turns = lambda: 6
        unsure = _quiet_params(bridge)
        await bridge.stay_quiet(unsure)
        assert "not_quiet" in unsure.results[-1][0]   # old evidence: plain words are for Yuki

    asyncio.run(main())


# -- E. "go quiet for a couple of hours" -----------------------------------------------------------------------

def _session(tmp: Path) -> Any:
    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "r.csv", voice_wake=False)
    session = VoiceSession(settings, QObject(), None, prefs_path=tmp / "voice.json")
    session.play_tone = lambda *a: None
    return session


def test_go_quiet_holds_across_a_restart_and_the_tray_can_undo_it(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.wake_enabled = True
    session._ensure_listener = lambda: None   # never a real listener in a test
    seen: list[float] = []
    session.quiet_changed.connect(seen.append)
    session.go_quiet(120)
    APP.processEvents()
    assert session.quiet and not session._wake_allowed()
    assert seen and abs(seen[-1] - (time.time() + 7200)) < 5
    again = _session(tmp_path)   # a restart: read back from voice.json
    again.wake_enabled = True
    assert again.quiet and not again._wake_allowed()
    again._ensure_listener = lambda: None
    again.wake_now()
    assert not again.quiet and again._wake_allowed()
    assert not _session(tmp_path).quiet   # and that is remembered too


def test_the_talkers_go_quiet_asks_the_session_and_ends_the_conversation() -> None:
    async def main() -> None:
        rt, bridge = _bridge()
        asked: list[float] = []
        ended: list[int] = []
        bridge.quiet_requested = asked.append
        bridge.end_requested = lambda: ended.append(1)
        bridge.spoke_in_reply = lambda: False
        p = Params()
        p.function_name = "go_quiet"
        p.arguments = {"say_first": "Sure, going quiet.", "minutes": 120}
        await bridge.go_quiet(p)
        assert asked == [120.0] and ended == [1]
        assert _run_llm(p.results[-1][1]) is False

    asyncio.run(main())
