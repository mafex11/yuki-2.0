"""Report 76 quick win 6: honest wording. "You're typing or clicking, I'll wait" only on measured input, and Yuki's own
internal words (the thinker, its tool names, a stray marker word) never heard and never on a card. Stubs only: no
keyboard, microphone, speaker or model is touched."""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest

from yuki.core.ownwords import TOOL_WORDS, internal_word, strip_internal

# -- the vocabulary ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, word", [
    ("The thinker's still messaging him.", "thinker"),
    ("Let me hand this to tell_yuki.", "tell_yuki"),
    ("Opening Chrome. act", "act"),
    ("act", "act"),
    ("Quiet:", "quiet"),
])
def test_internal_words_are_found(text, word):
    assert internal_word(text) == word


@pytest.mark.parametrize("text", [
    "I'll act on it now.", "Nothing to say.", "More.", "Your coach moved practice to six.",
    "Check secret_cases_holdout.py before you push.", "Opening Chrome.", "Keep it quiet tonight, okay?",
])
def test_everyday_words_pass(text):
    assert internal_word(text) is None and strip_internal(text) == text


def test_strip_internal_keeps_the_rest_of_a_card():
    assert strip_internal("Spotify is paused. The thinker is still clicking around.") == "Spotify is paused."
    assert strip_internal("Done. act") == "Done."
    assert strip_internal("The thinker's on it.") == ""


def test_every_tool_name_of_yukis_is_known():
    from yuki.agent.tools import ALL_TOOL_NAMES
    from yuki.voice.hands import HAND_TOOLS

    root = Path(__file__).resolve().parents[1] / "yuki" / "voice"
    voice = set(re.findall(r'"name": "([a-z_]+)"', (root / "mouth.py").read_text(encoding="utf-8")))
    names = set(ALL_TOOL_NAMES) | {t["name"] for t in HAND_TOOLS} | voice
    missing = sorted(n for n in names if "_" in n and n not in TOOL_WORDS)
    assert missing == []


# -- the voice: dropped in shadow mode too ----------------------------------------------------------------------------


def _guard(shadow: bool):
    from yuki.voice.speechguard import SpeechGuard

    async def judge(text, said, user):
        return "SAY"

    return SpeechGuard(None, "stub", judge=judge, shadow=shadow)


@pytest.mark.parametrize("shadow", [True, False])
def test_the_voice_never_says_an_internal_word(shadow):
    async def run():
        guard = _guard(shadow)
        dropped = await guard.allows("The thinker is still clicking around on that page.")
        lone = await guard.allows("act")
        kept = await guard.allows("Spotify is paused now, the volume is at half.")
        fut = guard.start("Let me pass this to tell_yuki now.")
        verdict = await guard.decide(fut, "Let me pass this to tell_yuki now.")
        return dropped, lone, kept, verdict, guard.stats

    dropped, lone, kept, verdict, stats = asyncio.run(run())
    assert dropped is False and lone is False and kept is True
    assert verdict.ok is False and verdict.by == "vocab" and verdict.why == "internals"
    assert stats["vocab"] == 3


def test_blocks_is_only_for_the_vocabulary():
    from yuki.voice.speechguard import SpeechGuard, Verdict

    async def run():
        loop = asyncio.get_running_loop()
        model_drop, vocab_drop, pending = loop.create_future(), loop.create_future(), loop.create_future()
        model_drop.set_result(Verdict(False, "reasoning", by="model"))
        vocab_drop.set_result(Verdict(False, "internals", by="vocab"))
        return SpeechGuard.blocks(model_drop), SpeechGuard.blocks(vocab_drop), SpeechGuard.blocks(pending)

    assert asyncio.run(run()) == (False, True, False)


def _conversation(pushed: list[str], unguarded: list[str]):
    from types import SimpleNamespace

    from yuki.config import Settings
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    class _Log:
        def __getattr__(self, name):
            return lambda *a, **k: None

    async def push(frame, direction=None):
        pushed.append(frame.text)

    conv = Conversation(settings=Settings(voice_qos=False, voice_priority_boost=False), bridge=SimpleNamespace(),
                        transport=None, memory=None, mouth_memory=MouthMemory(), log=_Log(), speech_end=lambda: None,
                        on_state=lambda *a: None, session="t")
    conv._llm = SimpleNamespace(push_frame=push)
    conv._tts = SimpleNamespace(unguard=unguarded.append)
    return conv


def test_lines_said_by_code_never_carry_an_internal_word():
    # review F1: a result's sentences and an owed outcome skip the speech guard (unguarded), so the vocabulary check
    # runs before they are said: "I tried open_url ..." is left out, the rest is said; nothing left -> nothing said
    pushed: list[str] = []
    unguarded: list[str] = []
    conv = _conversation(pushed, unguarded)
    asyncio.run(conv._speak_result(["I tried open_url but the page didn't load.", "The site is down for now."]))
    assert pushed == ["The site is down for now."] and unguarded == pushed
    asyncio.run(conv._speak_result(["The thinker is on it."]))
    asyncio.run(conv._speak_owed("Your export finished. The thinker logged it."))
    asyncio.run(conv._speak_owed("check_progress says done"))
    assert pushed == ["The site is down for now.", "Your export finished."] and unguarded == pushed


# -- cards ------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def store():
    from yuki.memory.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def test_a_card_never_carries_an_internal_word(store):
    from yuki.memory.store import NewNudge, NudgeCheckin

    _cid, nid = store.record_checkin(NudgeCheckin(at=1000.0, trigger="periodic", outcome="nudge"),
                                     NewNudge(kind="notice", text="Your export finished. The thinker logged it.",
                                              trigger="notice"))
    assert [n.text for n in store.nudges() if n.id == nid] == ["Your export finished."]
    _cid, none = store.record_checkin(NudgeCheckin(at=1001.0, trigger="periodic", outcome="nudge"),
                                      NewNudge(kind="notice", text="The thinker's still on it.", trigger="notice"))
    assert none is None and len(store.nudges()) == 1


# -- "you're typing or clicking" only on measured input ---------------------------------------------------------------


def _gate(**kw):
    from yuki.actions.leases import InputGate

    clock = {"t": 0.0}
    kw.setdefault("held", lambda: False)
    kw.setdefault("cursor", lambda: (500, 500))
    gate = InputGate(sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"], **kw)
    return gate, clock


def test_no_wait_line_on_an_input_whose_kind_was_never_measured():
    # the input that made the gate look (0.3 s before the action) is waited out, but no key or click was seen: silent
    gate, clock = _gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05, notify_after_s=0.0)
    gate.age = lambda: 0.3 + clock["t"]
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["ok"] and said == []


def test_the_wait_line_once_a_held_key_is_measured():
    presses = iter([True, True, True, True, True])     # the first look is a baseline; then a key is held
    gate, clock = _gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.1, notify_after_s=0.2,
                        held=lambda: next(presses, False))
    gate.age = lambda: 0.05 if clock["t"] < 0.5 else clock["t"] - 0.45   # they stop typing at 0.45 s
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["ok"] and result["real"] and len(said) == 1 and "typing or clicking" in said[0]


def test_a_moving_mouse_never_gets_the_line():
    gate, clock = _gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05, notify_after_s=0.0,
                        cursor=lambda: (int(clock["t"] * 1000), 300))
    gate.age = lambda: 0.01
    said: list[str] = []
    assert gate.wait(notify=said.append)["ok"] and said == []
