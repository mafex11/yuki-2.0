"""capC5 (report 78 §7.1, §7.6, §11 D3; report 102): the order, the A/B switch, safety and the caps.

When the user asks for something: (1) a qualifying way, run by run_skill (in parts when it has them); (2) otherwise
the thinker with hints - the usual path, go-tos and chords, pitfalls, the example, a way seen once; (3) otherwise the
thinker alone. A trial or a hint-only way is never run alone. The switch "use what Yuki learned from how you work"
(``[trace] use_learned``, default on) is read live: off, no way the trace taught, no go-tos, no paths. Sends still
need the user's words, and Yuki's own check sees what it saw before plus the picked item's name. The caps are as
they were.

Offline: temp databases, a stand-in desk, the stand-in embedder; invented apps, names and words.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from test_c_practice import vec
from test_capC2_links import NOW, store  # noqa: F401 - the store fixture
from test_capC4_demos import answer, chat_demo, demos, learner, taskrow
from test_capC4_skills import el
from test_capC5_guard import Desk5
from yuki.agent import frame as F
from yuki.memory import practice as P
from yuki.memory import trace_families
from yuki.memory import trace_habits as H
from yuki.memory import trace_replay as R
from yuki.voice import practice as VP
from yuki.voice import skills as S

WORDS = "send a message in a channel"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name, value in (("FIRST_FIND_WAIT_S", 0.0), ("FIND_WAIT_S", 0.0), ("AFTER_OPEN_WAIT_S", 0.0),
                        ("FOCUS_WAIT_S", 0.0), ("EXPECT_WAIT_S", 0.0), ("END_WAIT_S", 0.0), ("POLL_S", 0.0)):
        monkeypatch.setattr(S, name, value)
    R.set_use_learned(None)        # these tests read the settings file itself (the test run's own temp folder)
    _write_switch(None)
    yield
    _write_switch(None)
    R.set_use_learned(None)


def _write_switch(on: bool | None) -> None:
    from yuki.memory.privacy import user_config_path

    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if on is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(f"# test\n[trace]\nrecord_actions = true\nuse_learned = {'true' if on else 'false'}\n",
                    encoding="utf-8")


def _demo_way(store: Any, *, times: int = 2) -> P.Playbook:
    tasks = [chat_demo(store, NOW + k * 86400) for k in range(times)]
    lrn = learner(store, [answer(s, slots={s[2]: "channel", s[4]: "message"}) for _t, s in tasks])
    for k, (t, _s) in enumerate(tasks):
        lrn.learn_task(taskrow(store, t), now=NOW + k * 86400 + 120)
    [o] = demos(store)
    return o


def _talker(store: Any) -> VP.PracticeLookup:
    """The talker's lookup over this store (the memory service's ``practice``, the stand-in embedder)."""
    def practice(words: str, *, open_apps: Any = (), embed_wait_s: float = 0.4, detail: bool = False,
                 go_tos: bool = True) -> dict[str, Any]:
        out = P.relevant(store, query_vec=vec(words), apps_in_play=list(open_apps), detail=detail, go_tos=go_tos)
        return {**out, "measured": True}

    client = SimpleNamespace(practice=practice)
    lookup = VP.PracticeLookup(SimpleNamespace(call=lambda what, fn, timeout_s=1.0: fn(client)),
                               apps=lambda: ["chatter"])
    lookup.embed_ok, lookup.detail_ok = True, True
    lookup.ready.set()
    return lookup


def _ask(lookup: VP.PracticeLookup, words: str = WORDS) -> VP.TalkerPractice:
    one = VP.TalkerPractice(words=words, turn=0, started=0.0)
    lookup._lookup(one)
    return one


def _frame(store: Any, words: str = WORDS) -> F.TaskFrame:
    practice = P.relevant(store, query_vec=vec(words), apps_in_play=["chatter"])
    return F.build({}, practice=practice)


def _goto_rows(store: Any) -> None:
    from test_capC1b_gotos import act

    for k in range(3):
        act(store, NOW - 3000 + k, "chatter", "keys", resolved="focus", chord="Ctrl+K")
    H.update(store, now=NOW)


# -- (1) a qualifying way, run by run_skill ------------------------------------------------------------------------

def test_a_qualifying_way_is_offered_to_the_talkers_hands_with_its_steps(store):
    o = _demo_way(store)
    got = _ask(_talker(store))
    assert [s["id"] for s in got.skills] == [o.id]
    assert got.skills_text.startswith(VP.SKILLS_HEAD) and "steps=" in got.skills_text
    # the thinker sees it too (the way first, if the talker handed the request over)
    frame = _frame(store)
    assert f"[{o.ref}]" in frame.text and frame.procedures >= 1


def test_a_long_way_in_parts_gives_the_talker_its_typing_steps_rather_than_text(store):
    run = [{"do": "click", "role": "Button", "name": f"Step number {k} of a rather long invented way"}
           for k in range(7)]
    run.insert(3, {"do": "type", "slot": "message"})
    item = {"ref": "pb:7", "intent": "post the long update", "apps": ["chatter"], "label": "your way", "run": run,
            "parts": [{"run": run, "end": {"kind": "field_submitted"}}], "text": "Part 1: ..."}
    text = VP.skills_block([item])
    line = text.splitlines()[1]
    assert len(line) <= VP.SKILL_CHARS + 2
    assert 'steps=[{"do":"type","text":"<message, in their words>"}] (the other 7 steps run from memory)' in line
    # and the skill runner takes exactly that: the rest comes from memory
    steps = S.fill_slots(S.parse_steps([{"do": "type", "text": "hello"}]), run)
    assert [s.do for s in steps] == ["click"] * 3 + ["type"] + ["click"] * 4 and steps[3].text == "hello"


# -- (2) the thinker with hints ------------------------------------------------------------------------------------

def test_a_way_seen_once_is_a_hint_for_the_thinker_never_offered_or_run_alone(store):
    o = _demo_way(store, times=1)
    assert o.trial
    got = _ask(_talker(store))
    assert got.skills == [] and got.skills_text == ""
    frame = _frame(store)
    assert f"[{o.ref}]" in frame.text and "your way, seen once" in frame.text
    [w] = P.relevant(store, query_vec=vec(WORDS), apps_in_play=["chatter"], detail=True)["ways"]
    with pytest.raises(S.SkillRefused, match="seen only once"):
        S.SkillRunner(Desk5({}, {}, 0)).run({"steps": []}, item=w, words=[WORDS])
    with pytest.raises(S.SkillRefused, match="a hint for the thinker"):
        S.SkillRunner(Desk5({}, {}, 0)).run({"steps": []}, item={**w, "trial": False, "hint_only": True},
                                            words=[WORDS])


def test_without_a_way_the_thinker_gets_the_path_the_go_tos_with_chords_and_the_pitfalls(store, monkeypatch):
    _goto_rows(store)
    P.add_playbook(store, {"kind": "pitfall", "intent": "send in a chat", "apps": ["chatter"],
                           "body": {"text": "a pasted mention stays text", "instead": "type it by keys"}},
                   events=[{"type": "observed", "run_key": "r1"}], vector=vec("send in a chat"))
    monkeypatch.setattr(trace_families, "hints", lambda *a, **k: {
        "path": {"family": "messaging in a chat app", "tasks": 4, "steps": ["Press Ctrl+K in chatter",
                                                                            "Type the channel"]},
        "example": None, "family_id": None})
    frame = _frame(store)
    assert "How the user usually does this" in frame.text and "1. Press Ctrl+K in chatter" in frame.text
    assert "presses Ctrl+K (3 times" in frame.text
    assert F.PITFALL_HEAD in frame.text and frame.pitfalls == 1
    assert _ask(_talker(store)).skills == []          # nothing for the talker's hands: the thinker's


def test_with_nothing_learned_the_thinker_works_alone(store):
    frame = _frame(store)
    assert F.PLAYBOOK_HEAD not in frame.text and F.GOTO_HEAD not in frame.text and "usually does this" not in \
        frame.text
    assert _ask(_talker(store)).skills == []


# -- the A/B switch, read live -------------------------------------------------------------------------------------

def test_the_switch_off_removes_learned_ways_go_tos_and_paths_and_on_brings_them_back_without_a_restart(store,
                                                                                                    monkeypatch):
    o = _demo_way(store)
    _goto_rows(store)
    called = []
    monkeypatch.setattr(trace_families, "hints", lambda *a, **k: called.append(1) or {
        "path": {"family": "messaging", "tasks": 3, "steps": ["Press Ctrl+K"]}, "example": None, "family_id": None})
    ev = [{"type": "observed", "run_key": "r1"}, {"type": "succeeded", "run_key": "r1"}]
    plain = P.add_playbook(store, {"kind": "procedure", "intent": "send a message in a channel by the menu",
                                   "apps": ["chatter"], "body": {"steps": ["Menu", "Send"]}}, events=ev,
                           vector=vec("send a message in a channel by the menu"))
    on = P.relevant(store, query_vec=vec(WORDS), apps_in_play=["chatter"], detail=True)
    way_item = next(w for w in on["ways"] if w["id"] == o.id)
    assert way_item["trace"] is True and on["go_tos"] and on["path"]
    assert VP.qualifies(way_item)

    _write_switch(False)                                  # the user's settings file changes; nothing restarts
    off = P.relevant(store, query_vec=vec(WORDS), apps_in_play=["chatter"], detail=True)
    assert [w["id"] for w in off["ways"]] == [plain]       # a way learned from Yuki's own runs stays
    assert "go_tos" not in off and off.get("path") is None
    assert not VP.qualifies(way_item)                     # an answer from before the switch moved is not offered
    with pytest.raises(S.SkillRefused, match="switched off"):
        S.SkillRunner(Desk5({}, {}, 0)).run({"steps": []}, item=way_item, words=[WORDS])
    frame = F.build({}, practice=off)
    assert f"[{o.ref}]" not in frame.text and F.GOTO_HEAD not in frame.text and "usually does this" not in frame.text
    assert R.health_words({}, on=R.use_learned()).startswith("; using what it learned from how you work is "
                                                             "switched off")

    _write_switch(True)
    back = P.relevant(store, query_vec=vec(WORDS), apps_in_play=["chatter"], detail=True)
    assert o.id in [w["id"] for w in back["ways"]] and back["go_tos"] and VP.qualifies(way_item)


def test_the_switch_defaults_on_and_a_broken_file_keeps_the_last_good_value(tmp_path):
    R.set_use_learned(None)
    path = tmp_path / "privacy.toml"
    assert R.use_learned(path) is True                     # no file: the default, on
    path.write_text("[trace]\nuse_learned = false\n", encoding="utf-8")
    assert R.use_learned(path) is False
    path.write_text("[trace\nuse_learned = ", encoding="utf-8")   # half written
    assert R.use_learned(path) is False                    # the last good value
    path.write_text("[trace]\nrecord_actions = true\n", encoding="utf-8")
    assert R.use_learned(path) is True                     # no key: the default
    from pathlib import Path

    packaged = (Path(P.__file__).parent / "privacy_default.toml").read_text(encoding="utf-8")
    assert "\nuse_learned = true\n" in packaged


# -- safety unchanged ----------------------------------------------------------------------------------------------

def _send_way() -> dict[str, Any]:
    run = [{"do": "pick", "in": {"role": "List"}, "role": "ListItem", "app": "chatter", "slot": "chat"},
           {"do": "type", "slot": "message"}, {"do": "keys", "keys": ["enter"], "app": "chatter"}]
    return {"ref": "pb:5", "id": 5, "intent": "send a message to someone", "apps": ["chatter"], "text": "",
            "parts": P.clean_parts([{"run": run, "end": {"kind": "field_submitted", "app": "chatter"}}]),
            "run": run, "sends": True, "demo": True, "trace": True}


def _chat_desk() -> Desk5:
    tree = [el(1, "List", "Chats"), el(11, "ListItem", "Mira Okafor", parent=1),
            el(12, "ListItem", "Team room", parent=1), el(20, "Edit", "Message")]
    return Desk5({100: {"title": "Chatter", "process_name": "chatter"}}, {100: tree}, 100)


def test_sends_still_need_the_users_words_and_the_check_sees_the_picked_name():
    R.set_use_learned(True)
    words = ["send see you at noon to Mira Okafor"]
    steps = [{"do": "pick", "text": "Mira Okafor"}, {"do": "type", "text": "see you at noon"}]
    with pytest.raises(S.SkillRefused, match="their own words"):
        S.SkillRunner(_chat_desk()).run({"steps": steps, "sends": False}, item=_send_way(), words=words)
    with pytest.raises(S.SkillRefused, match="not what they said"):
        S.SkillRunner(_chat_desk()).run({"steps": steps, "sends": True, "to": "Mira Okafor",
                                         "what": "see you at one"}, item=_send_way(), words=words)
    desk = _chat_desk()
    ok, _r, facts = S.SkillRunner(desk).run({"steps": steps, "sends": True, "to": "Mira Okafor",
                                             "what": "see you at noon"}, item=_send_way(), words=words)
    assert ok, facts
    tools = [j[0] for j in desk.judged]
    assert tools == ["click", "type_text", "hotkey"]          # every step judged, as before
    pick = desk.judged[0]
    assert '"Mira Okafor"' in pick[2]["element"] and "Mira Okafor" in pick[1]["element"]
    assert desk.judged[1][1]["text"] == "see you at noon" and desk.judged[2][1]["keys"] == ["enter"]


# -- caps unchanged ------------------------------------------------------------------------------------------------

def test_the_caps_are_unchanged():
    assert (S.MAX_STEPS, S.MAX_PARTS, S.MAX_KEYS) == (8, 4, 4)
    assert (VP.SKILL_CHARS, VP.LESSONS_CHARS, VP.MAX_SKILLS, VP.MAX_LESSONS) == (520, 400, 2, 3)
    assert (VP.SKILL_MIN_COSINE, VP.SKILL_MIN_CONFIDENCE) == (0.56, 0.70)
    assert (F.FRAME_CHARS, F.MAX_PROCEDURES, F.MAX_PITFALLS) == (2_500, 2, 3)
    assert (F.MAX_GOTO_APPS, F.MAX_GOTO_ELEMENTS, F.MAX_GOTO_CHORDS, F.MAX_PATH_STEPS) == (3, 5, 3, 8)
    assert set(S.RUN_SKILL_TOOL["properties"]) == {"say_first", "skill", "steps", "sends", "to", "what",
                                                   "say_when_done"}
    assert S.RUN_SKILL_TOOL["required"] == ["say_first", "skill", "steps", "sends", "say_when_done"]
    # a chord's words fit inside the go-tos line's own per-chord room
    assert H.DID_CHARS == 110
    assert "go_tos" in inspect.signature(P.relevant).parameters
