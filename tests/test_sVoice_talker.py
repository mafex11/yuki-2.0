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


# -- review round 2 ------------------------------------------------------------------------------------------------------

#: An invented volume connection (no real app): the volume skill with a no-focus mixer rung and a focus rung B.
VOLUME_TOML = """\
[connection]
app = "demoaudio"

[skill.volume]
end = "volume_change"
kind = "change"
menu = "set an invented app's level"
done_line = "Volume set."
handoff = true

[[skill.volume.slot]]
name = "level"
type = "number"
min = 0
max = 100
required = true
ask = "What level?"

[[skill.volume.slot]]
name = "control"
type = "enum"
options = ["in_app", "mixer"]
default = "in_app"

[[skill.volume.route]]
rung = "A"
variant = "mixer"
via = "app_volume"
when = { slot = "control", in = ["mixer"] }
args = { app = "DemoAudio.exe", level = "{level}" }
game = "always"
done = { probe = "read_back" }

[[skill.volume.route]]
rung = "B"
variant = "steps"
via = "uia_steps"
args = { apps = ["DemoAudio.exe"], steps = [{ do = "click", role = "Button", name = "Demo level up" }] }
done = { probe = "level", target = "DemoAudio.exe", value = "{level}", within_s = 1 }
"""


class SlowMemory:
    """A memory that never answers in time (the reviewer's test_r2_menu_timeout.py)."""

    def __init__(self) -> None:
        self.asked = 0

    def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
        self.asked += 1
        raise TimeoutError(f"{what}: memory did not answer within {timeout_s} s")

    def context(self, **kwargs: Any) -> Any:
        raise TimeoutError("memory did not answer")


def volume_folder(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The invented volume connection beside sCore's Demo Player, as the loaded connection files."""
    from pathlib import Path

    from yuki.skills import connection as C

    folder = Path(tmp_path) / "connections"
    folder.mkdir()
    (folder / "audio.toml").write_text(VOLUME_TOML, encoding="utf-8")
    fixtures = Path(__file__).with_name("skills_fixtures")
    (folder / "demo.toml").write_text((fixtures / "demo.toml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(C, "CONNECTIONS_DIR", folder)
    C.clear_cache()
    assert C.load().get("volume") is not None, C.load().errors
    return folder


@pytest.fixture()
def volume_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from yuki.skills import connection as C

    yield volume_folder(tmp_path, monkeypatch)
    C.clear_cache()


def test_a_menu_read_that_times_out_keeps_the_volume_skill_and_never_brings_the_mixer_hand_back(volume_dir: Any) -> None:
    """Review r2 M1: with skills on and no menu from memory (a timeout), the talker's menu comes from the bundled
    connection files alone: run_skill with volume, no app_volume and no volume keys, and not the old hands block."""
    from yuki.voice import hands as H

    menu, source = M.talker_menu(SlowMemory(), timeout_s=0.01)
    assert source == "fallback" and menu and "volume" in M.menu_keys(menu)
    tools = H.talker_hand_tools(M.menu_keys(menu))
    names = [t["name"] for t in tools]
    assert "app_volume" not in names and "run_skill" in names
    run = next(t for t in tools if t["name"] == "run_skill")
    assert "volume" in run["properties"]["skill"]["enum"]
    media = next(t for t in tools if t["name"] == "media_key")
    assert not set(media["properties"]["key"]["enum"]) & {"volumeup", "volumedown", "volumemute"}
    block = M.hands_block(menu)
    assert block != M.HANDS_PROMPT and "app_volume" not in block and "run_skill volume" in block
    assert M.talker_menu(SlowMemory(), timeout_s=0.01) == (menu, "fallback")       # deterministic: cache-stable
    # memory failing outright at the conversation's start (its context read): the same, and memory is not asked again
    slow = SlowMemory()
    got = M.fetch_memory(slow, timeout_s=0.01)
    assert got.skills == menu and got.skills_source == "fallback" and slow.asked == 0
    assert got.facts()["skills_source"] == "fallback"


def test_the_fallback_is_logged_and_the_conversation_gets_the_skill_hand(volume_dir: Any) -> None:
    from yuki.config import Settings
    from yuki.voice.pipeline import Conversation

    menu, source = M.talker_menu(SlowMemory(), timeout_s=0.01)
    events: list[tuple[str, dict]] = []
    log = SimpleNamespace(event=lambda kind, **fields: events.append((kind, fields)))
    c = SimpleNamespace(settings=Settings(), bridge=SimpleNamespace(hands=object()), memory=object(), log=log,
                        session="s", mouth_memory=M.MouthMemory(skills=menu, skills_source=source))
    c._skill_keys = lambda: Conversation._skill_keys(c)
    names = [t["name"] for t in Conversation._talker_hand_tools(c)]
    assert "run_skill" in names and "app_volume" not in names
    kind, fields = events[-1]
    assert kind == "skill_menu" and fields["source"] == "fallback" and fields["skills"] >= 1


def test_the_volume_line_stays_on_a_menu_memory_built_without_it(volume_dir: Any) -> None:
    """With skills on and the volume connection loaded, the volume skill is the talker's only volume hand: a memory
    menu without its line (the ledger, or the cap) gets it back, first."""
    without = MENU.replace(MENU.splitlines()[1] + "\n", "")

    class Mem:
        def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
            return fn(SimpleNamespace(skill_menu=lambda audience: without))

    menu, source = M.talker_menu(Mem())
    assert source == "memory" and M.menu_keys(menu)[0] == "volume" and "spotify.playlist" in M.menu_keys(menu)


class _TasteRig:
    """A temp MemoryClient over sCore's Demo Player connection with invented lists in its taste table."""

    def __init__(self, tmp_path: Any) -> None:
        from pathlib import Path

        from yuki.memory.api import MemoryClient
        from yuki.memory.store import Store

        self.client = MemoryClient(Store.open(Path(tmp_path) / "memory.db"))

    def add(self, entries: list[tuple[str, str, list[float], int]]) -> None:
        import json
        import time

        st = self.client.store

        def go(conn: Any) -> None:
            for key, name, hist, n in entries:
                conn.execute("INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, "
                             "hist_weekday, hist_weekend, n_obs, updated_at) VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?)",
                             ("demo_list", key, st._enc(name), st._enc(""), json.dumps(hist), json.dumps(hist), n,
                              time.time()))

        st._write(go)

    def memory(self) -> Any:
        client = self.client

        class Mem:
            def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
                return fn(client)

        return Mem()


def test_the_talker_menu_is_byte_identical_through_the_day_and_rebuilt_the_next(tmp_path: Any,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r2 M2 (the reviewer's test_r2_menu_hour.py): the menu sits in the cached hands block, so it is one
    snapshot per day (PLAN.md B.2 "rebuilt nightly only"), built with the names most seen first (``n_obs``): the same
    bytes at 09:00 and 21:00 (even rebuilt from scratch); a name played during the day waits for the next day."""
    from datetime import datetime
    from pathlib import Path

    from yuki.skills import connection as C

    monkeypatch.setattr(C, "CONNECTIONS_DIR", Path(__file__).with_name("skills_fixtures"))
    C.clear_cache()
    rig = _TasteRig(tmp_path)
    try:
        morning = [0.0] * 24
        morning[9] = 10.0
        evening = [0.0] * 24
        evening[21] = 10.0
        rig.add([("k1", "Invented Morning List", morning, 5), ("k2", "Invented Evening List", evening, 7)])
        day = datetime(2026, 10, 14)   # a Wednesday
        at9, at21 = day.replace(hour=9).timestamp(), day.replace(hour=21).timestamp()
        mem = rig.memory()
        m9, s9 = M.talker_menu(mem, now=at9)
        m21, s21 = M.talker_menu(mem, now=at21)
        assert (s9, s21) == ("memory", "snapshot") and m9 == m21
        assert M.hands_block(m9) == M.hands_block(m21)
        rebuilt, source = M.talker_menu(rig.memory(), now=at21)   # another memory object: built again, same bytes
        assert source == "memory" and rebuilt == m9
        line = next(l for l in m9.splitlines() if "demo.play" in l)
        assert line.index("Evening") < line.index("Morning")       # most seen first, whatever the hour
        rig.add([("k3", "Invented New List", morning, 50)])        # played a lot today: tomorrow's menu
        assert M.talker_menu(mem, now=day.replace(hour=23).timestamp()) == (m9, "snapshot")
        before_four = datetime(2026, 10, 15, 3).timestamp()        # the day turns at MENU_DAY_HOUR
        assert M.talker_menu(mem, now=before_four) == (m9, "snapshot")
        tomorrow, source = M.talker_menu(mem, now=datetime(2026, 10, 15, 9).timestamp())
        assert source == "memory" and "Invented New List" in tomorrow and tomorrow != m9
    finally:
        rig.client.close()
        C.clear_cache()


def test_a_slow_memory_on_a_new_day_keeps_the_last_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime
    from pathlib import Path

    from yuki.skills import connection as C

    monkeypatch.setattr(C, "CONNECTIONS_DIR", Path(__file__).with_name("skills_fixtures"))
    C.clear_cache()

    class Flaky:
        slow = False

        def call(self, what: str, fn: Any, timeout_s: float = 1.0) -> Any:
            if self.slow:
                raise TimeoutError(what)
            return fn(SimpleNamespace(skill_menu=lambda audience: MENU))

    try:
        mem = Flaky()
        first, source = M.talker_menu(mem, now=datetime(2026, 10, 14, 9).timestamp())
        assert source == "memory" and first == MENU
        mem.slow = True
        assert M.talker_menu(mem, now=datetime(2026, 10, 15, 9).timestamp()) == (MENU, "stale")
        # another cap: the snapshot doesn't fit it, the store-free fallback (sCore's three demo skills)
        menu, source = M.talker_menu(mem, chars=300, now=datetime(2026, 10, 15, 9).timestamp())
        assert source == "fallback" and M.menu_keys(menu)[0] == "demo.volume" and len(menu) <= 300
    finally:
        C.clear_cache()


def test_heard_words_follow_pipecats_real_orders() -> None:
    """Review r2 L5: a reply that spoke and then called a tool (Pipecat keeps the call, its placeholder, then the
    words), and a reply the user cut off (only what was heard is kept): the hand-off's words are the user's since."""
    from yuki.voice.bridge import exact_words, heard_words

    spoke_then_called = [
        {"role": "user", "content": "open youtube"},
        {"role": "assistant", "content": "YouTube's up."},
        {"role": "user", "content": "message him on whatsapp"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "tell_yuki"}}]},
        {"role": "tool", "content": "IN_PROGRESS"},
        {"role": "assistant", "content": "Sure, one sec."},
    ]
    ctx = SimpleNamespace(get_messages=lambda: spoke_then_called)
    assert heard_words(ctx, since_reply=True) == ["message him on whatsapp"]
    cut_off = [
        {"role": "user", "content": "open spotify"},
        {"role": "assistant", "content": "Opening Spot"},       # cut off by the user: what was heard
        {"role": "user", "content": "no wait, play my focus playlist"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "tell_yuki"}}]},
        {"role": "tool", "content": "IN_PROGRESS"},
    ]
    ctx = SimpleNamespace(get_messages=lambda: cut_off)
    assert exact_words(ctx) == "no wait, play my focus playlist"
    assert heard_words(ctx) == ["open spotify", "no wait, play my focus playlist"]   # both in the heard-words note
    answer = [
        {"role": "user", "content": "message him on whatsapp"},
        {"role": "assistant", "content": "Which chat?"},
        {"role": "user", "content": "the bro chat"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "tell_yuki"}}]},
    ]
    assert exact_words(SimpleNamespace(get_messages=lambda: answer)) == "the bro chat"
    # the answer alone is the first line, so the prompt and the schema name the request it completes as context
    assert "the request their answer completes" in M.MOUTH_PROMPT
    assert "answer to your question completes" in M.TELL_YUKI["properties"]["context"]["description"]
