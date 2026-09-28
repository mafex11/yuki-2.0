"""Voice v3.13 (2026-09-29, the 01:25-01:32 session: "it was not that good, check the entire chat and fix the things
that it did wrong").

Covered offline: the spoken-line check fails closed for long lines, checks a reply's short first words (a fragment is
dropped) and drops the rest of a reply once one piece was caught; spelled-out letters are one word (WARP) resolved
against the names on the screen; the talker's silence stands while a hand opens what they just confirmed; no
acknowledgement once the outcome was said; the after-goodbye and Speech notes are private; maximized is not full
screen (the window hand's maximize no longer crashes, full screen is F11 checked against the monitor, the overview
says which); an app that shows no items (Warp) gets its recent window titles as its names, for recognition, the talker
and the thinker; the voice log never writes on the voice loop.
No microphone, no speakers, no network, no real window touched: fakes and stand-ins only.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QCoreApplication

from test_voice_v310 import _guard, _quiet_params
from test_voice_v39 import _bridge

APP = QCoreApplication.instance() or QCoreApplication([])
W = lambda s: [{"word": w, "confidence": 0.97} for w in s.split()]  # noqa: E731


# -- 1 and 8: the spoken-line check ----------------------------------------------------------------------------

def test_a_replys_short_first_words_are_checked_and_a_fragment_is_dropped() -> None:
    async def main() -> None:
        guard = _guard({"or something else?": "DROP: fragment"})
        verdict = await guard.decide(guard.start("or something else?", first=True), "or something else?")
        assert not verdict.ok and verdict.why == "fragment" and guard.seen == ["or something else?"]
        later = await guard.decide(guard.start("Which one?"), "Which one?")   # not a first piece: free, no model
        assert later.ok and later.by == "free"

    asyncio.run(main())


def test_the_rest_of_a_reply_is_dropped_once_a_piece_of_it_was_caught() -> None:
    """Live 01:26:32: of four pieces of the talker's notes the check caught one; the other three were heard."""
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service

    async def main() -> None:
        async with FakeServers() as fake:
            tts = _service(fake)
            guard = _guard(delay=0.1)

            async def judge(text: str, said: list[str], user: str) -> str:   # only the first piece is caught
                guard.seen.append(text)
                await asyncio.sleep(0.1)
                return "DROP: reasoning" if "asked to stop" in text else "SAY"

            guard.judge = judge
            tts.set_guard(guard)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("I was asked to stop listening a moment ago. "),
                      LLMTextFrame("These words are only the start of a sentence here."), LLMFullResponseEndFrame(),
                      SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert _levels(down) == []   # nothing heard
            assert not [f for f in down if isinstance(f, TTSTextFrame)]
            assert tts.stats["guarded"] == 2 and guard.stats["caught"] == 2

    asyncio.run(main())


# -- 2: spelled letters ------------------------------------------------------------------------------------------

def test_spelled_letters_are_one_word_resolved_against_the_names_known() -> None:
    from yuki.voice.pipeline import heard_note

    note, _low, near = heard_note(W("Open w a r p."), ["Warp"], screen=[])
    assert "'w a r p' is a word spelled out letter by letter: WARP, which is Warp" in note
    assert near == 0   # a known name spelled exactly: nothing to doubt, nothing held
    note, _low, near = heard_note(W("In w a r p window, open the l l l m o s chart."), [],
                                  screen=["LLM OS agent harness revamp", "Corvana pull request 2781"])
    assert "LLLMOS, most likely LLM OS agent harness revamp" in note and near >= 1
    assert heard_note(W("I need a new tab"), [], screen=[])[0] is None   # one lone letter is a word


def test_the_speech_note_and_the_after_goodbye_note_are_private() -> None:
    from yuki.voice.mouth import volatile_block

    text = volatile_block(heard="'w a r p' is a word spelled out letter by letter: WARP", after_goodbye=True)["text"]
    assert "private, never mentioned to them" in text
    assert "Private, never to be said: this conversation had ended with a goodbye" in text
    assert "They told you to stop listening" not in text   # read out live as "I was asked to stop listening"


# -- 3 and 4: one outcome, no acknowledgement after it --------------------------------------------------------------

def test_silence_stands_while_a_hand_opens_what_they_just_confirmed() -> None:
    """Live 01:26:55: "Yeah." after "Open Warp?" had its stay_quiet refused while the hand was still opening Warp; the
    talker then said "Warp is open." and the hand's own line "Warp's up." followed."""
    async def main() -> None:
        _rt, bridge = _bridge()
        bridge.user_turn = lambda: True
        bridge.spoke_in_reply = lambda: False
        bridge.in_exchange = lambda: True
        bridge.user_turns = lambda: 4
        bridge.busy_tools = 1   # the open_app hand is running
        quiet = _quiet_params(bridge)
        await bridge.stay_quiet(quiet)
        assert quiet.results[-1][0] == {"ok": True}
        bridge.busy_tools = 0
        bridge.user_turns = lambda: 5
        again = _quiet_params(bridge)
        await bridge.stay_quiet(again)
        assert "not_quiet" in again.results[-1][0]   # nothing on its way: a live exchange still gets words

    asyncio.run(main())


def test_no_acknowledgement_once_the_outcome_was_said() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        bridge.user_turns = lambda: 7
        assert bridge.may_ack()
        bridge._outcome_said(SimpleNamespace(user_turn=6))   # an older request's outcome: this one may still be acked
        assert bridge.may_ack()
        bridge._outcome_said(SimpleNamespace(user_turn=7))
        assert not bridge.may_ack()

    asyncio.run(main())


# -- 6: maximized is not full screen --------------------------------------------------------------------------------

class _FakeUser32:
    def __init__(self, zoomed: bool = False) -> None:
        self.zoomed = zoomed

    def IsZoomed(self, hwnd: int) -> int:  # noqa: N802 - the Win32 name
        return int(self.zoomed)

    def GetForegroundWindow(self) -> int:  # noqa: N802
        return 77


def _fake_window(monkeypatch: Any, user32: _FakeUser32, full: dict) -> Any:
    import yuki.actions.launch as launch

    monkeypatch.setattr(launch, "_user32", user32)
    monkeypatch.setattr(launch.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(launch.win32gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(launch.win32gui, "ShowWindow", lambda h, cmd: setattr(user32, "zoomed", True))
    monkeypatch.setattr(launch, "window_info", lambda h: SimpleNamespace(process_name="warp.exe", title="Warp"))
    monkeypatch.setattr(launch, "_is_fullscreen", lambda h: full["on"])
    return launch


def test_maximize_is_verified_and_says_it_is_not_full_screen(monkeypatch: Any) -> None:
    """Live 01:28:11 every maximize by the window hand failed: pywin32's win32gui has no IsZoomed."""
    launch = _fake_window(monkeypatch, _FakeUser32(), {"on": False})
    result = launch.window_action(77, "maximize")
    assert result.ok and "is now maximized (not full screen)" in result.summary


def test_full_screen_is_f11_checked_against_the_monitor(monkeypatch: Any) -> None:
    full = {"on": False}
    launch = _fake_window(monkeypatch, _FakeUser32(zoomed=True), full)
    import yuki.actions.input as actions_input

    pressed: list[tuple] = []

    def hotkey(*keys: str, expect_hwnd: Any = None) -> Any:
        pressed.append(keys)
        full["on"] = True
        return SimpleNamespace(ok=True, summary="pressed f11")

    monkeypatch.setattr(actions_input, "hotkey", hotkey)
    result = launch.window_action(77, "fullscreen")
    assert result.ok and pressed == [("f11",)] and "is now full screen" in result.summary

    full["on"] = False
    monkeypatch.setattr(actions_input, "hotkey", lambda *k, expect_hwnd=None: SimpleNamespace(ok=True, summary="ok"))
    monkeypatch.setattr(launch, "_FULLSCREEN_WAIT_S", 0.05)
    stuck = launch.window_action(77, "fullscreen")
    assert not stuck.ok and "still maximized, not full screen" in stuck.summary   # never claimed


def test_the_overview_says_maximized_or_full_screen() -> None:
    from yuki.perception.windows import DesktopOverview, WindowInfo, format_overview

    windows = [WindowInfo(hwnd=1, title="Warp", process_name="warp.exe", pid=1, is_foreground=True,
                          is_minimized=False, bounds=(0, 0, 1920, 1040), is_maximized=True),
               WindowInfo(hwnd=2, title="Video", process_name="chrome.exe", pid=2, is_foreground=False,
                          is_minimized=False, bounds=(0, 0, 1920, 1080), is_fullscreen=True)]
    text = format_overview(DesktopOverview(windows=windows, foreground_hwnd=1, screen_size=(1920, 1080),
                                           virtual_bounds=(0, 0, 1920, 1080)))
    lines = text.splitlines()
    assert any("[1]" in ln and "MAXIMIZED (not full screen)" in ln for ln in lines)
    assert any("[2]" in ln and "FULL SCREEN" in ln for ln in lines)


# -- 7: an app that shows no items: its recent titles ---------------------------------------------------------------

def test_recent_titles_are_an_apps_own_most_used_first_without_status_glyphs() -> None:
    from yuki.memory.api import MemoryClient
    from yuki.memory.store import app_key

    key = app_key("warp.exe")
    row = lambda title, s: SimpleNamespace(process=key, title=title, active_s=s, passive_s=0.0)  # noqa: E731
    asked: list[tuple] = []

    class Store:
        def timeline_of(self, process: str, since: Any = None) -> list:
            asked.append((process, since))
            return [row("✳ Corvana pull request 2781", 30), row("LLM OS agent harness revamp", 600),
                    row("◑ LLM OS agent harness revamp", 100), row("Corvana pull request 2781", 10)]

    client = MemoryClient.__new__(MemoryClient)
    client.store = Store()
    assert client.recent_titles("warp.exe") == ["LLM OS agent harness revamp", "Corvana pull request 2781"]
    assert asked and asked[0][0] == key   # only that app's rows are read


def test_an_app_that_shows_no_items_is_named_by_its_recent_titles() -> None:
    from yuki.voice.hands import Hands

    class Desk:
        def get_window_tree(self, hwnd: int, **_: Any) -> dict:   # Warp: its title bar's buttons only
            return {"elements": [{"id": 1, "role": "Button", "name": "Restore", "depth": 1}]}

    hands = Hands(Desk())
    windows = [{"hwnd": 9, "process_name": "warp.exe", "title": "✳ Corvana pull request 2781",
                "bounds": (0, 0, 1920, 1040)}]
    hands._dispatcher = SimpleNamespace(activity_facts=lambda: {"media": []},
                                        overview_text=lambda: ("windows", {"windows": windows, "foreground_hwnd": 9},
                                                               0.0))
    got: list = []
    titles = ["LLM OS agent harness revamp", "Corvana pull request 2781"]
    hands.refresh_screen_names(got.append, titles=lambda image: titles if image == "warp.exe" else [])
    deadline = time.monotonic() + 10   # the app name is resolved once, cold
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    assert got == [["Restore", *titles]] and hands.screen_titles == titles
    snap = hands.snapshot()
    assert "LLM OS agent harness revamp" in snap.text and "speech recognition" not in snap.text

    async def thinker_line() -> str:
        _rt, bridge = _bridge()
        bridge.hands = SimpleNamespace(last=SimpleNamespace(foreground=9, tabs={}, tab_ids={}), screen_titles=titles,
                                       screen_hwnd=9)
        return bridge.tabs_for_thinker()

    line = asyncio.run(thinker_line())
    assert line.startswith("window [9] exposes no tabs to read") and '"LLM OS agent harness revamp"' in line

    from yuki.voice.mouth import brain_request

    request = brain_request("Switch to the LLM OS rigid hardness one", ["Switch to the LLM OS rigid hardness one."],
                            tabs=line)
    assert "reading its title after each step" in request and "The tab tool takes these ids" not in request


# -- 4: the voice loop never waits on the disk ------------------------------------------------------------------------

def test_the_voice_log_writes_off_the_loop(tmp_path: Path) -> None:
    from yuki.voice.log import VoiceLog

    log = VoiceLog(tmp_path / "voice")
    log.event("off_loop", n=1)   # another thread: written at once
    path = next((tmp_path / "voice").glob("voice-*.jsonl"))
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["type"] == "off_loop"

    async def main() -> None:
        log.event("on_loop", n=2)   # the loop's thread: handed to the writer
        log.turn({"session": "s", "turn": 1})

    asyncio.run(main())
    assert log._writer is not None and log._writer.name == "yuki-voice-log"
    log.flush()
    kinds = [json.loads(x)["type"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["off_loop", "on_loop", "turn"]
    assert (tmp_path / "voice_turns.csv").exists()
