"""sVoice: the talker's prompt, its exact words to the thinker, the menu in the hands block, the old learning path off
(PLAN.md Phase S S-1, D10, D15; B.2 "Talker cache"; map-skills S3.1, S3.3, S5).

No model call: the prompt's texts and the hand-off format only (token counts before and after are measured live in
the build report, not here). No app, window, key or sound.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.voice import mouth as M

MENU = ("Skills (the user's usual ways; run_skill with the key, slots only from their words or a name listed):\n"
        "- volume: change a volume [target] [change] [level] [mute] [control: in_app | mixer]\n"
        "- spotify.playlist: play one of their Spotify playlists [playlist: Lo-fi | Focus]")


def _all_prompt_text() -> str:
    parts = [M.MOUTH_PROMPT, M.VOICE_MARKS_PROMPT, M.HANDS_PROMPT, M.RECOGNITION_GUIDE, M.WORK_GUIDE,
             M.HAND_TROUBLE_NOTE, M.REOPEN_GUIDE, M.FOCUS_GUIDE, M.SPEECH_CHANNEL_PROMPT, M.CALL_LAYER,
             M.CALL_LAYER_UNSURE, M.CUT_OFF_GUIDE, M.hands_prompt(["volume", "spotify.playlist"])]
    return "\n".join(parts)


# -- D10: the talker passes the user's exact words; no paraphrased request field --------------------------------------


def test_tell_yuki_has_no_request_field_and_no_prompt_example_writes_one() -> None:
    assert "request" not in M.TELL_YUKI["properties"]
    assert "request" not in M.TELL_YUKI["required"]
    assert set(M.TELL_YUKI["required"]) == {"screen", "say_first", "app_named", "intent"}
    text = _all_prompt_text()
    assert "request=" not in text
    assert not re.search(r'request "[A-Z]', text)   # no example hands over a written request any more


def test_the_brain_request_starts_with_the_users_own_words_and_keeps_the_heard_mark() -> None:
    from yuki.agent.memory import VOICE_HEARD_MARK, memory_query_of

    text = M.brain_request("put something on youtube im having lunch", ["hm", "put something on youtube im having "
                                                                         "lunch"], context="'it' = the video")
    assert text.splitlines()[0] == "put something on youtube im having lunch"
    assert VOICE_HEARD_MARK in text and "their words win over the request's wording" not in text
    assert "[References in their words" in text
    assert "youtube" in memory_query_of(text)   # the agent's own parser still finds the heard words


def test_heard_words_since_reply_are_the_users_turns_after_yukis_last_reply() -> None:
    from yuki.voice.bridge import exact_words, heard_words

    messages = [
        {"role": "user", "content": "open youtube"},
        {"role": "assistant", "content": "YouTube's up."},
        {"role": "user", "content": "bro did you see that"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "stay_quiet"}}]},
        {"role": "user", "content": "message hemanth on whatsapp"},
        {"role": "developer", "content": "async result"},
        {"role": "user", "content": "say I'm on my way"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "tell_yuki"}}]},
        {"role": "tool", "content": "IN_PROGRESS"},
    ]
    ctx = SimpleNamespace(get_messages=lambda: messages)
    assert heard_words(ctx, since_reply=True) == ["message hemanth on whatsapp", "say I'm on my way"]
    assert exact_words(ctx) == "message hemanth on whatsapp say I'm on my way"   # "WhatsApp" survives (3 Oct)
    assert heard_words(ctx) == ["bro did you see that", "message hemanth on whatsapp", "say I'm on my way"]
    lone = SimpleNamespace(get_messages=lambda: [{"role": "assistant", "content": "Hey"}])
    assert exact_words(lone, heard=["the earlier words"]) == "the earlier words"


# -- the Skills menu in the hands block; still 4 breakpoints; no state grows -------------------------------------------


def test_the_menu_is_appended_to_the_hands_block_and_the_breakpoints_stay_two() -> None:
    memory = M.MouthMemory(names=["Sam"], standing="- be brief", source="service", skills=MENU)
    blocks = M.mouth_system_blocks(memory, channel="marker")
    hands = blocks[1]["text"]
    assert hands.startswith("# Your hands") and hands.endswith(MENU)
    assert "cache_control" in blocks[1]          # breakpoint 1 stays on the hands block (it carries the menu)
    assert sum(1 for b in blocks if b.get("cache_control")) == 2   # + Pipecat's 2 = 4, never a 5th
    without = M.mouth_system_blocks(M.MouthMemory(names=["Sam"], standing="- be brief", source="service"))
    assert without[1]["text"] == M.HANDS_PROMPT  # no menu: the hands block as before phase S


def test_the_skills_variant_replaces_lessons_and_volume_and_is_not_longer() -> None:
    for part in (M._VOLUME_OLD, M._LESSONS_OLD, M._LEARNED_WAY_OLD, M._PLAYLIST_OLD, M._VOLUME_EXAMPLES_OLD,
                 M._LEARNED_EXAMPLE_OLD, M._SPOTIFY_EXAMPLE_OLD, M._INSTANT_OLD):
        assert M.HANDS_PROMPT.count(part) == 1, part[:60]
    both = M.hands_prompt(["volume", "spotify.playlist"])
    assert "Lessons line" not in both and "Learned ways" not in both and M.SKILLS_PARAGRAPH.strip() in both
    assert "app_volume" not in both and "volumeup" not in both and "volumedown" not in both
    assert "run_skill volume" in both and 'skill="spotify.playlist"' in both
    assert len(both) + len(MENU) < len(M.HANDS_PROMPT) + 400   # the menu replaces removed text
    only_playlist = M.hands_prompt(["spotify.playlist"])
    assert "app_volume(" in only_playlist          # without the volume skill the mixer hand keeps its words
    assert M.hands_prompt([]) == M.HANDS_PROMPT


def test_the_guides_word_volume_as_the_skill_only_when_it_is_on_the_menu() -> None:
    focus = M.volatile_block(state={"focus": {"game": "VALORANT", "call": False}, "skill_keys": ("volume",)})["text"]
    assert 'skill="volume"' in focus and 'key="volumeup"' not in focus
    old = M.volatile_block(state={"focus": {"game": "VALORANT", "call": False}})["text"]
    assert 'key="volumeup"' in old
    work = M.volatile_block(work="task 1 running", state={"skill_keys": ("volume",)})["text"]
    assert "app_volume" not in work and "run_skill volume work any time" in work


def test_skills_now_rides_in_the_volatile_block_at_most_300_chars() -> None:
    text = M.volatile_block(desk="[1] app.exe \"x\"", state={"skills_now": "Skills now: " + "a" * 500})["text"]
    line = next(l for l in text.splitlines() if l.startswith("[Skills now"))
    assert len(line) <= 302 and text.index("[Skills now") < text.index(M.DESK_LABEL)


def test_fetch_skills_reads_the_menu_and_drops_anything_else() -> None:
    class Mem:
        def __init__(self, menu: Any) -> None:
            self.menu = menu

        def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
            return fn(SimpleNamespace(skill_menu=lambda audience: self.menu))

    assert M.fetch_skills(Mem(MENU)) == MENU
    assert M.fetch_skills(Mem("")) is None and M.fetch_skills(Mem(None)) is None
    assert M.fetch_skills(Mem("not a menu")) is None
    assert M.menu_keys(MENU) == ["volume", "spotify.playlist"]
    big = MENU.split("\n")[0] + "\n" + "\n".join(f"- skill{i}: {'x' * 120}" for i in range(40))
    assert len(M.fetch_skills(Mem(big))) <= M.SKILLS_CHARS


# -- D15: the old learning path is off beside the skill path; back only with skills = false --------------------------


def test_the_old_learning_path_is_off_by_default_and_never_beside_skills() -> None:
    from yuki.config import Settings
    from yuki.voice.pipeline import practice_on, skills_on

    s = Settings()
    assert s.skills is True and s.voice_practice is False and s.voice_skills is False
    assert skills_on(s) and not practice_on(s)
    assert not practice_on(Settings(voice_practice=True))                 # never beside the skill path
    assert practice_on(Settings(voice_practice=True, skills=False))       # back only with skills = false
    assert not skills_on(Settings(skills=False))
    assert (s.skills_max, s.skills_menu_chars, s.skills_disable_after, s.skills_volume_step) == (20, 2400, 2, 10)
    from yuki import skills

    for key, value in skills.DEFAULTS.items():
        assert getattr(s, key) == value                                    # one source of the defaults


def test_the_conversation_builds_its_hands_from_the_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.config import Settings
    from yuki.voice.pipeline import Conversation

    def conv(settings: Any, menu: str | None) -> Any:
        c = SimpleNamespace(settings=settings, bridge=SimpleNamespace(hands=object()), memory=object(), log=None,
                            session="s", mouth_memory=M.MouthMemory(skills=menu))
        c._skill_keys = lambda: Conversation._skill_keys(c)
        return c

    on = conv(Settings(), MENU)
    tools = Conversation._talker_hand_tools(on)
    names = [t["name"] for t in tools]
    assert "run_skill" in names and "keep_usual" in names and "app_volume" not in names
    off = conv(Settings(skills=False), MENU)
    tools = Conversation._talker_hand_tools(off)
    assert "app_volume" in [t["name"] for t in tools] and "run_skill" not in [t["name"] for t in tools]
    assert off.mouth_memory.skills is None            # the menu is dropped: the hands block as before phase S
    old = conv(Settings(skills=False, voice_practice=True, voice_skills=True), None)
    assert "run_skill" in [t["name"] for t in Conversation._talker_hand_tools(old)]   # the step-list hand, old path


# -- review round 1 ------------------------------------------------------------------------------------------------------


def test_a_sound_only_run_skill_is_never_held_on_unsure_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r1 L3: the volume skill (it replaced app_volume and the volume keys, which were never held) and a menu
    skill whose every rung takes no focus run at once on unsure words; a skill with a focus rung, a skill not on the
    menu and the old learned-way hand are held as before."""
    from pathlib import Path

    from yuki.skills import connection as C
    from yuki.voice.pipeline import Conversation

    monkeypatch.setattr(C, "CONNECTIONS_DIR", Path(__file__).with_name("skills_fixtures"))
    C.clear_cache()
    try:
        c = SimpleNamespace(_skill_keys=lambda: ["volume", "demo.volume", "demo.home"])
        quiet = lambda args: Conversation._sound_skill(c, "run_skill", args)   # noqa: E731
        assert quiet({"skill": "volume"})                       # the volume skill
        assert not quiet({"skill": "demo.home"})                # its rung opens a URI (takes focus)
        assert not quiet({"skill": "demo.volume"})              # its rung B clicks
        assert not quiet({"skill": "demo.play"})                # not on this conversation's menu
        assert not Conversation._sound_skill(c, "click", {"skill": "volume"})
        assert not quiet(None)
        c_none = SimpleNamespace(_skill_keys=lambda: [])          # no menu: the old learned-way hand stays held
        assert not Conversation._sound_skill(c_none, "run_skill", {"skill": "volume"})
    finally:
        C.clear_cache()


def test_the_menu_cap_reads_the_setting_and_volume_wording_names_mute() -> None:
    class Mem:
        def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
            return fn(SimpleNamespace(skill_menu=lambda audience: MENU))

    assert M.fetch_skills(Mem()) == MENU
    first_two = "\n".join(MENU.splitlines()[:2])
    assert M.fetch_skills(Mem(), chars=len(first_two) + 2) == first_two   # Settings.skills_menu_chars, whole lines
    assert "mute (on or off)" in M.VOLUME_PARAGRAPH


def test_held_lets_the_volume_skill_through_and_holds_a_window_skill() -> None:
    from yuki.voice.pipeline import Conversation

    call = SimpleNamespace(first_text=0.1, user_turn=True)
    c = SimpleNamespace(_llm=SimpleNamespace(_yuki_hold={}, _yuki_call=call),
                        observer=SimpleNamespace(heard=True, heard_plain=False),
                        _skill_keys=lambda: ["volume", "no.such.skill"])
    c._sound_skill = lambda name, args: Conversation._sound_skill(c, name, args)
    assert not Conversation._held(c, "run_skill", None, {"skill": "volume", "slots": {"change": "down"}})
    assert Conversation._held(c, "run_skill", None, {"skill": "no.such.skill", "slots": {}})   # unknown: held
    assert Conversation._held(c, "click", None, {"element": 3})
    assert not Conversation._held(c, "keep_usual", None, {"answer": "yes"})
