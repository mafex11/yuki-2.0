"""Voice v3.12 (2026-09-28, the 13:26-13:32 session): offline checks.

A hand that finishes silently (no "Done." after every scroll); after a barge-in nothing of the old reply plays
(pre-fetched pieces included) and the talker is told what was heard and what was not said; one acknowledgement per
request; an outcome of an older request is folded in unsaid once a newer request exists; a half-written mark never
reaches the voice; the wake listener in its own process keeps up while the app is busy; the user's own sites reach
the talker. No microphone, no speakers: fakes, a localhost fake Gemini, a pretend microphone.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from test_voice_v39 import Params, _bridge, _hand_params, _hands, _run_llm, _settle


# -- 1. a hand that finishes silently ----------------------------------------------------------------------------

class _ScrollDesk:
    def __init__(self, moves: bool = True) -> None:
        self.moves = moves

    def scroll(self, x: int, y: int, **_: Any) -> Any:
        return SimpleNamespace(ok=self.moves, summary="scrolled Document down: 10% -> 20%" if self.moves
                               else "already at the bottom: nothing to scroll",
                               details={"verified": self.moves}, elapsed_ms=5.0)

    def activity_facts(self) -> dict:
        return {"media": []}

    def get_desktop_overview(self) -> dict:
        return {"windows": [], "foreground_hwnd": 7}

    def format_overview(self, overview: dict) -> str:
        return ""


def _scroll_hands(desk: Any, bridge: Any) -> Any:
    from yuki.voice.hands import Snapshot

    hands = _hands(desk, bridge)
    hands.last = Snapshot(text="x", hwnds={7}, foreground=7, bounds={7: (0, 0, 800, 600)})
    hands._dispatcher = SimpleNamespace(activity_facts=desk.activity_facts,
                                        overview_text=lambda: ("", {"foreground_hwnd": 7}, 0.0))
    return hands


def test_a_verified_hand_with_an_empty_done_line_says_nothing() -> None:
    from pipecat.frames.frames import TTSSpeakFrame

    async def main() -> None:
        _rt, bridge = _bridge()
        hands = _scroll_hands(_ScrollDesk(), bridge)
        hands._act = lambda name, args, facts: (True, "scrolled Document down: 10% -> 20%",
                                                 {**facts, "verified": True, "sent_at": time.monotonic()})
        params = _hand_params("scroll", {"window": 7, "direction": "down", "say_when_done": ""})
        await hands.handle(params)
        result, props = params.results[-1]
        assert result.get("said") == "" and _run_llm(props) is False
        assert not [f for f in params.pushed if isinstance(f, TTSSpeakFrame)]   # silent: they watched it move

    asyncio.run(main())


def test_a_hand_that_did_not_work_is_never_silent() -> None:
    async def main() -> None:
        _rt, bridge = _bridge()
        hands = _scroll_hands(_ScrollDesk(moves=False), bridge)
        hands._act = lambda name, args, facts: (False, "already at the bottom: nothing to scroll",
                                                 {**facts, "verified": False, "settled": True})
        params = _hand_params("scroll", {"window": 7, "direction": "down", "say_when_done": ""})
        await hands.handle(params)
        result, props = params.results[-1]
        assert "said" not in result and _run_llm(props) is not False   # the talker reads the report and says it

    asyncio.run(main())


# -- 2. barge-in ------------------------------------------------------------------------------------------------

def test_after_a_barge_in_no_prefetched_piece_of_the_old_reply_plays() -> None:
    from pipecat.frames.frames import (
        InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame,
        TTSTextFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service

    first = "Do you mean the subscriptions list in the sidebar,"
    second = "or something else on the Sidemen page?"

    async def main() -> None:
        async with FakeServers() as fake:
            fake.delays = {first: 0.0, second: 0.8}   # the second piece's audio arrives after they spoke
            tts = _service(fake)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame(first + " "), LLMTextFrame(second),
                      LLMFullResponseEndFrame(), SleepFrame(0.4), InterruptionFrame(), SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            levels = _levels(down)
            assert levels == [1000 + 37 * len(first)]   # only the first piece was ever heard
            texts = [f.text for f in down if isinstance(f, TTSTextFrame)]
            assert not any("Sidemen page" in t for t in texts)   # and never kept as said
            assert not tts._replies

    asyncio.run(main())


def _observer() -> Any:
    from yuki.config import Settings
    from yuki.voice.pipeline import TurnObserver

    return TurnObserver(settings=Settings(), log=None, memory=None, session="t", speech_end=lambda: None,
                        on_state=lambda s, d: None, tts_provider="gemini", activity=lambda: None)


def test_the_talker_is_told_what_was_heard_and_what_was_not_said() -> None:
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, LLMTextFrame, TTSTextFrame
    from pipecat.frames.frames import LLMFullResponseStartFrame, UserStartedSpeakingFrame

    obs = _observer()
    out = object()
    obs.output_processor = out
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMTextFrame("The first request writes the cache, "))
    obs.on_frame(LLMTextFrame("and after that every request reads it."))
    obs.on_frame(BotStartedSpeakingFrame())
    heard = TTSTextFrame("The first request writes the cache,", aggregated_by="sentence")
    obs.on_played(SimpleNamespace(frame=heard, source=out))
    obs.on_frame(UserStartedSpeakingFrame())   # they spoke over it, right as the next sentence began
    obs.turn.audible_at = time.monotonic() + 1.0   # (no time to hear any of it)
    obs.on_frame(BotStoppedSpeakingFrame())
    cut = obs.cut_for_now()
    assert cut == {"heard": "The first request writes the cache,", "unsaid": "and after that every request reads it."}
    obs.user_turns += 2   # two turns later it is no longer news
    assert obs.cut_for_now() is None


def test_the_cut_line_is_in_the_talkers_volatile_block() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, volatile_block

    text = volatile_block(cut={"heard": "Do you mean the list,", "unsaid": "or something else?"})["text"]
    assert "They heard: \"Do you mean the list,\"" in text and "Not said yet: \"or something else?\"" in text
    assert "# When they cut you off" in MOUTH_PROMPT


# -- 3. one acknowledgement, outcomes bound to the latest request ---------------------------------------------------

def test_one_acknowledgement_per_request() -> None:
    from pipecat.frames.frames import TTSSpeakFrame

    async def main() -> None:
        rt, bridge = _bridge()
        turns = {"n": 4}
        bridge.user_turns = lambda: turns["n"]
        bridge.spoke_in_reply = lambda: False
        a = Params("Open devmafex dot com", call_id="a")
        a.arguments["say_first"] = "My bad, opening DevMafex now."
        await bridge._ack_if_silent(a, a.arguments)
        b = Params("Open devmafex dot com", call_id="b")
        b.arguments["say_first"] = "Opening it."
        await bridge._ack_if_silent(b, b.arguments)
        said = [f.text for p in (a, b) for f in p.pushed if isinstance(f, TTSSpeakFrame)]
        assert said == ["My bad, opening DevMafex now."]
        turns["n"] = 5   # their next words: a new acknowledgement may be said
        c = Params("Close the other tab", call_id="c")
        await bridge._ack_if_silent(c, c.arguments)
        assert [f.text for f in c.pushed if isinstance(f, TTSSpeakFrame)] == ["On it."]

    asyncio.run(main())


def test_an_older_outcome_is_folded_in_unsaid_once_a_newer_request_exists() -> None:
    async def main() -> None:
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
        job_old, job_new = rt.submitted[0][0], rt.submitted[1][0]
        rt.started.emit("worker", job_old, "x")
        rt.finished.emit("worker", job_old, "Accounts list is expanded.")
        rt.lane_done.emit("worker", job_old, "x", False, [])
        await _settle(20)
        await asyncio.wait_for(t1, 1)
        result, props = old.results[-1]
        assert "held_back" in result and props.run_llm is False   # not said as the answer to "Add another account"
        rt.started.emit("worker", job_new, "x")
        rt.finished.emit("worker", job_new, "Opening the add account page now.")
        rt.lane_done.emit("worker", job_new, "x", False, [])
        await _settle(20)
        await asyncio.wait_for(t2, 1)
        result, props = new.results[-1]
        assert result.get("result") == "Opening the add account page now." and props.run_llm is True

    asyncio.run(main())


# -- 4. marks and goodbyes ------------------------------------------------------------------------------------------

def test_a_half_written_mark_never_reaches_the_voice() -> None:
    from yuki.voice.pipeline import drop_unfinished_marks

    assert drop_unfinished_marks("[") == ""
    assert drop_unfinished_marks("Sure, later! [lau") == "Sure, later! "
    assert drop_unfinished_marks("[laugh] No way.") == "[laugh] No way."
    assert drop_unfinished_marks("Hmm <sigh") == "Hmm "


def test_a_wake_right_after_a_goodbye_is_judged_not_taken_as_addressed() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, volatile_block

    assert "stop listening a moment ago" in volatile_block(after_goodbye=True)["text"]
    assert "never say one without the call" in MOUTH_PROMPT
    assert "I had a long talk" in MOUTH_PROMPT   # talk about Yuki is not calling her


# -- 5. the user's own sites reach the talker --------------------------------------------------------------------------

def test_the_users_own_sites_are_in_the_talkers_rules_layer() -> None:
    from yuki.voice.mouth import mouth_rules

    standing = ("[Standing rules and open commitments, from Yuki's conversations with the user]\n"
                "Rules the user set for Yuki:\n- [rule:1] keep it short\n"
                "The user's own sites, accounts and links, spelled as confirmed:\n"
                "- [resource:3] portfolio (devmafex.com)\n"
                "Yuki's own commitments (what Yuki promised or was asked to do later):\n- [commitment:4] remind at 5")
    rules = mouth_rules(standing)
    assert "devmafex.com" in rules and "keep it short" in rules and "remind at 5" not in rules


# -- 6. the wake listener in its own process ---------------------------------------------------------------------------

def test_the_process_listener_forwards_audio_and_reports_the_name() -> None:
    """The real child process with the real spotter, fed a one-breath clip through the pipe (no device)."""
    from yuki.voice.audio import RATE, SharedMic
    from yuki.voice.transport import load_clip
    from yuki.voice.wake import DEFAULT_MODEL, MODELS, MODELS_DIR
    from yuki.voice.wakeproc import ProcessWakeListener

    if not (MODELS[DEFAULT_MODEL].path(MODELS_DIR) / MODELS[DEFAULT_MODEL].encoder).exists():
        pytest.skip("wake-word model not downloaded")
    clip = load_clip(Path(__file__).parent / "data" / "voice" / "one_breath_pause.wav")
    mic = SharedMic.__new__(SharedMic)
    SharedMic.__init__(mic, None)
    hits: list[tuple[float, Any]] = []
    listener = ProcessWakeListener(mic, lambda at, kw: hits.append((at, kw)), check=False)
    listener.start()
    try:
        assert listener.ready.wait(60)
        audio = np.concatenate([np.zeros(RATE // 2, np.float32), clip, np.zeros(RATE, np.float32)])
        t0 = time.monotonic()
        n = RATE // 50
        for i in range(0, len(audio), n):
            target = t0 + i / RATE
            while time.monotonic() < target:
                time.sleep(0.002)
            mic.feed(audio[i:i + n], target)
        deadline = time.monotonic() + 3
        while not hits and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hits and hits[0][1] == "YUKI"
        assert listener.name_end is not None and t0 + 0.5 < listener.name_end < t0 + 0.5 + len(clip) / RATE
    finally:
        listener.stop()
    assert not listener.running


# -- 7. timed quiet never turns listening off for good (v3.13) ---------------------------------------------------------

def _session(tmp: Path) -> Any:
    from PySide6.QtCore import QObject

    from yuki.config import Settings
    from yuki.voice.session import VoiceSession

    session = VoiceSession(Settings(log_dir=tmp / "sessions", requests_csv=tmp / "r.csv"), QObject(), None,
                           prefs_path=tmp / "voice.json")
    session.play_tone = lambda *a: None
    session._ensure_listener = lambda: None   # never a real listener
    session._stop_listener = lambda: None
    return session


def test_a_timed_quiet_never_persists_wake_off(tmp_path: Path) -> None:
    import json

    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    s = _session(tmp_path)
    s.go_quiet(60)
    app.processEvents()
    prefs = json.loads((tmp_path / "voice.json").read_text(encoding="utf-8"))
    assert prefs.get("wake", True) is True and prefs["quiet_until"] > time.time()
    assert s.listening_state == "quiet" and not s._wake_allowed()


def test_boot_after_the_quiet_ran_out_listens_again(tmp_path: Path) -> None:
    import json

    (tmp_path / "voice.json").write_text(json.dumps({"wake": True, "quiet_until": time.time() - 5}), encoding="utf-8")
    s = _session(tmp_path)
    assert s.listening_state == "on" and s._wake_allowed()
    assert "quiet_until" not in json.loads((tmp_path / "voice.json").read_text(encoding="utf-8"))


def test_only_the_users_own_switch_turns_listening_off_and_turning_it_on_ends_a_quiet(tmp_path: Path) -> None:
    import json

    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    s = _session(tmp_path)
    s.go_quiet(120)
    app.processEvents()
    s.set_wake(True)   # the window's key during the quiet: "listen now"
    assert s.listening_state == "on" and not s.quiet
    s.set_wake(False)  # the user's explicit off: remembered, said plainly
    assert s.listening_state == "off"
    assert json.loads((tmp_path / "voice.json").read_text(encoding="utf-8"))["wake"] is False
    assert _session(tmp_path).listening_state == "off"   # a restart keeps the user's own off


def test_stop_listening_alone_ends_the_conversation_and_is_never_quiet_time() -> None:
    from yuki.voice.mouth import GO_QUIET, MOUTH_PROMPT

    assert "you keep listening for it" in MOUTH_PROMPT
    assert "use end_conversation for those" in GO_QUIET["description"]
