"""sVoice: the bridge hands the thinker the user's exact words (PLAN.md D10) and a skill's hand-off with the skill
pinned (map-skills S3.4). A stand-in runtime; no app, window, key or sound; no model call."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])


class Runtime(QObject):
    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[tuple[int, str, dict]] = []

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
               view_hwnd: Any = None, memory_query: Any = None, user_words: Any = None,
               skill: dict | None = None) -> tuple[str, int]:
        job = len(self.submitted) + 1
        self.submitted.append((job, text, {"lane": lane, "user_words": user_words, "skill": skill}))
        return "worker", job

    def steer(self, job_id: int, text: str) -> bool:
        return False

    def cancel(self, job_id: int) -> str:
        return "running"


class Params:
    def __init__(self, args: dict, messages: list[dict]) -> None:
        self.arguments = args
        self.tool_call_id = "call-1"
        self.function_name = "tell_yuki"
        self.context = SimpleNamespace(get_messages=lambda: messages)
        self.results: list[tuple[dict, Any]] = []
        self.llm = SimpleNamespace(push_frame=self._push)

    async def _push(self, frame: Any, direction: Any = None) -> None:
        pass

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


async def _settle() -> None:
    for _ in range(8):
        APP.processEvents()
        await asyncio.sleep(0)


TURNS = [{"role": "user", "content": "what's the time"}, {"role": "assistant", "content": "Ten past four."},
         {"role": "user", "content": "message hemanth on whatsapp"}, {"role": "user", "content": "say on my way"}]


def test_tell_yuki_hands_over_the_users_words_never_a_paraphrase() -> None:
    from yuki.voice.bridge import VoiceBridge

    async def main() -> None:
        rt = Runtime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        # an older talker that still writes a paraphrase: ignored, the user's own words go
        params = Params({"screen": False, "say_first": "On it.", "intent": "new_task", "acts": True,
                         "request": "Message Hemanth asking if he is coming"}, TURNS)
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job, text, kw = rt.submitted[0]
        assert text.splitlines()[0] == "message hemanth on whatsapp say on my way"
        assert "asking if he is coming" not in text
        assert kw["user_words"] == ["what's the time", "message hemanth on whatsapp", "say on my way"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def test_a_skill_that_did_not_get_there_goes_to_the_thinker_pinned() -> None:
    from yuki.voice.bridge import VoiceBridge

    async def main() -> None:
        rt = Runtime()
        bridge = VoiceBridge(rt, foreground=lambda: 7)
        bridge.attach(asyncio.get_running_loop())
        pin = {"key": "volume", "goal": "change a volume", "slots": {"target": "Spotify"}, "tried": []}
        params = Params({"skill": "volume", "slots": {"target": "Spotify"}}, TURNS[:2] + [
            {"role": "user", "content": "turn spotify down a bit"}])
        assert await bridge.escalate(params, "run_skill", params.arguments, "its routes didn't get there", skill=pin)
        await _settle()
        job, text, kw = rt.submitted[0]
        assert kw["skill"] == pin and kw["lane"] == "worker"
        assert text.splitlines()[0] == "turn spotify down a bit"
        assert "their usual way (volume)" in text

    asyncio.run(main())


def test_a_held_hand_off_runs_on_the_users_words_too() -> None:
    from yuki.voice.bridge import VoiceBridge

    async def main() -> None:
        rt = Runtime()
        bridge = VoiceBridge(rt, foreground=lambda: None)
        bridge.attach(asyncio.get_running_loop())
        context = SimpleNamespace(get_messages=lambda: TURNS[:2] + [{"role": "user", "content": "open the brochure"}])
        assert await bridge._run_held(("tell_yuki", {"say_first": "", "screen": True, "intent": "new_task",
                                                     "context": "'brochure' = the Bro chat"}, context))
        await _settle()
        _job, text, _kw = rt.submitted[0]
        assert text.splitlines()[0] == "open the brochure" and "the Bro chat" in text

    asyncio.run(main())
