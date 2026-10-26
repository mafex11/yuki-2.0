"""Voice v3.18 (the design notes): what is left of the talker's use of practice memory. The voice turn's log, the
run_skill hand's schema and prompt words, the personal brief hook, the hands' own authorization check (the judged call
of a tab close, a request per user turn, no judge without an agent behind the voice), the finish contract of the
hand-off note, the heard words passed apart, and a lost speaker in a call.

kClean / iVoice / iMem (18 Oct) deleted the rest: hand traces read back from practice memory, the Lessons and Learned
ways lines, learned ways run as the talker's hand, Yuki's own voice on the app volumes line (the Situation's Sound line
marks it now), the request pack under the loop, and the stateful desks of scripts/practice_eval.py and
scripts/voice_procedures_eval.py that most of those tests ran on.

Offline only: stand-in desks (:class:`yuki.voice.offline.BrowserDesk`), stand-in memory; no microphone, no speakers, no
real desktop, no real memory, no model call.
"""

from __future__ import annotations

import asyncio
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
from yuki.voice.offline import BrowserDesk

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


# -- 1. the voice turn's log ---------------------------------------------------------------------------------

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


# -- 2. the talker's prompt, its tools, its own voice -------------------------------------------------------------

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


# -- 3. the personal brief ---------------------------------------------------------------------------------------

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


# -- 4. Yuki's own authorization check on the talker's hands (revamp D S1) -----------------------------------------

class FakeJudge:
    """``Authorizer.check`` as the hands call it: gated by the thinker's tool metadata (here: closes and keys)."""

    def __init__(self, allow: bool = True, question: str = "Close Chrome? You asked me to open it.") -> None:
        self.allow, self.question = allow, question
        self.calls: list[tuple[str, dict, list, dict, list]] = []
        self.requests = 0

    def begin_request(self) -> None:
        self.requests += 1

    def check(self, name: str, tool_input: dict, *, words: list, target: dict | None = None, signature: str = "",
              earlier: list | None = None) -> Any:
        gated = (name in ("hotkey", "press") or (name, tool_input.get("action")) in (("window_action", "close"),
                                                                                       ("tab", "close")))
        if not gated:
            return None
        self.calls.append((name, dict(tool_input), list(words), dict(target or {}), list(earlier or [])))
        return SimpleNamespace(allowed=self.allow, decision="allow" if self.allow else "ask_user",
                               question="" if self.allow else self.question, why="their words did not ask for it")


def test_closing_tabs_is_checked_with_the_tab_names_and_a_new_user_turn_is_a_new_request() -> None:
    from yuki.voice.hands import judged_call

    assert judged_call("tab", {"window": 5, "action": "close", "tabs": [2]}, tab_names=["Inbox"]) == (
        "tab", {"hwnd": 5, "action": "close", "tabs": ["Inbox"]})
    assert judged_call("media_key", {"key": "volumedown"}) is None
    assert judged_call("open_yuki", {"page": "Today"}) is None
    assert judged_call("open_app", {"name": "Chrome"}) == ("launch_app", {"query": "Chrome"})
    hands = _hands(BrowserDesk(action_delay_s=0))   # any stand-in desk: the stateful one went with its script
    judge = FakeJudge()
    hands._authorizer = judge
    hands.new_user_turn()
    hands.new_user_turn()
    assert judge.requests == 2


def test_without_a_judge_in_this_build_or_an_agent_behind_the_voice_the_hands_run_as_before() -> None:
    hands = _hands(BrowserDesk(action_delay_s=0), bridge=None)   # Chrome in front, as the old stateful desk had it
    assert hands._judge() is None and hands.authorize("window_action", {"action": "close"}) is None
    agentless = _hands(BrowserDesk(action_delay_s=0), bridge=SimpleNamespace(runtime=SimpleNamespace(worker=None)))
    assert agentless._judge() is None


def test_the_hand_off_note_follows_the_thinkers_finish_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.voice.mouth as mouth

    monkeypatch.setattr(mouth, "_FINISH_CONTRACT", "boolean")
    lean = mouth.brain_request("Minimize Discord", ["minimize discord"])
    assert "finishes_with true" in lean and "finish_words only when" in lean and "never to a quick click" in lean
    assert "Give each action or slower read its say_now" not in lean
    monkeypatch.setattr(mouth, "_FINISH_CONTRACT", "string")
    old = mouth.brain_request("Minimize Discord", ["minimize discord"])
    assert "worded as the outcome" in old and "Give each action or slower read its say_now" in old
    monkeypatch.setattr(mouth, "_FINISH_CONTRACT", None)
    assert mouth.finish_contract() in ("boolean", "string")   # probed from the agent's own schema


# -- 5. the 34-review fixes (H1, H4, H5, M7) ---------------------------------------------------------------------

class WordsRuntime(FakeRuntime):
    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
               view_hwnd: Any = None, memory_query: Any = None, user_words: Any = None) -> tuple[str, int]:
        return super().submit(text, origin_hwnd=origin_hwnd, source=source, lane=lane, view_hwnd=view_hwnd,
                              memory_query=memory_query, user_words=user_words)


def test_a_hand_off_passes_what_speech_recognition_heard_apart_as_the_users_words() -> None:
    """34-review H4: the talker's request is its reading of the words and may carry content it made up ("message
    Sam" -> "Message Sam asking when he joins tonight"); the heard turns go apart, labelled as the user's own words, for
    the judge (only when the runtime names ``user_words``)."""
    from test_voice_v39 import Params as TellParams

    async def main(rt: Any) -> list:
        _rt, bridge = _bridge(rt)
        task = asyncio.create_task(bridge.tell_yuki(TellParams("Message Sam asking when he joins tonight",
                                                               heard="message sam")))
        await _settle()
        task.cancel()
        return rt.submitted

    got = asyncio.run(main(WordsRuntime()))
    # vfix V4: plain strings, the runtime's contract (as {"how", "text"} dicts they reached the judge as Python reprs)
    assert got[-1][2]["user_words"] == ["message sam"]
    assert "user_words" not in asyncio.run(main(FakeRuntime()))[-1][2]


def test_a_lost_speaker_in_a_call_is_never_moved_to_speakers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """34-review M7: in a call (or with voice_require_headphones) the default output takes over only when it is
    headphones; otherwise the conversation ends with speaker_gone, so Yuki never plays into the call's microphone."""
    from test_voice_v316 import _Speaker, _session, _wait_for

    for headphones in (False, True):
        monkeypatch.setattr("yuki.voice.audio.headphones_check",
                            lambda *a, h=headphones, **k: (h, {"form_factor": "headphones" if h else "speakers"}))
        session, _played = _session(tmp_path, voice_echo="full")
        speaker = _Speaker()
        session._speaker, session.speaker_used = speaker, "Headphones (WH-XB900N)"
        session._thread = SimpleNamespace(is_alive=lambda: True)
        session._mic = SimpleNamespace(is_open=True, device=None, stats=SimpleNamespace(blocks=1),
                                       set_echo=lambda g: None, device_used="Mic")
        session.call_app = "Discord"
        stops: list[str] = []
        session.stop_conversation = lambda reason="user", wait_s=0.0: stops.append(reason)
        session._output_target = lambda chosen: chosen
        session._on_devices_seen({"devices": {"capture": [], "render": ["Speakers (Realtek)"]}, "stalled": False})
        assert _wait_for(lambda: not session._rerouting and (bool(stops) or bool(speaker.switched)))
        if headphones:
            assert stops == [] and speaker.switched == [None]
        else:
            assert stops == ["speaker_gone"] and speaker.switched == []
        session._thread = None
