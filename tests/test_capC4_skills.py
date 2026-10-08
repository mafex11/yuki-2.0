"""capC4 (report 78 §6.2, §7; report 100): run_skill runs ways learned from what the user did - in parts, each part's
checked end verified before the next, a failed end stopping the run with a handover - and the new ``pick`` step
(an item of a list found by the keyed digest of its name, or by the request's words). A trial is never run alone, a
Warp command never, a send only with the user's words. The caps stay as they were.

Offline: a stand-in desk (windows, trees, a dispatcher), a temp database for the digests; no real desktop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from test_capC2_links import store  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import trace_demos as TD
from yuki.voice import skills as S


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    for name, value in (("FIRST_FIND_WAIT_S", 0.0), ("FIND_WAIT_S", 0.0), ("AFTER_OPEN_WAIT_S", 0.0),
                        ("FOCUS_WAIT_S", 0.0), ("EXPECT_WAIT_S", 0.0), ("END_WAIT_S", 0.0), ("POLL_S", 0.0)):
        monkeypatch.setattr(S, name, value)


class Desk:
    """Windows, their trees and a dispatcher whose clicks may bring another window or page to the front."""

    def __init__(self, windows: dict[int, dict[str, Any]], trees: dict[int, list[dict[str, Any]]], front: int,
                 memory: Any = None) -> None:
        self.windows, self.trees, self.front = windows, trees, front
        self.memory = memory
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.effects: dict[Any, Any] = {}      # element id or ("keys", "ctrl+s") -> callable
        self._dispatcher = self

    # the dispatcher
    def overview_text(self) -> tuple[str, dict[str, Any], float]:
        return "", {"windows": [{"hwnd": h, **w} for h, w in self.windows.items()], "foreground_hwnd": self.front}, 0.0

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        key = args.get("element") if name == "click" else (name, "+".join(args.get("keys") or []))
        effect = self.effects.get(key)
        if effect is not None:
            effect()
        return SimpleNamespace(ok=True, payload={"details": {"verified": True}}, summary="done", elapsed_ms=1.0)

    # the hands
    def _now_board(self) -> dict[str, Any]:
        return {}

    def _read_tree(self, hwnd: int) -> Any:
        if hwnd not in self.trees:
            return None
        return {"elements": self.trees[hwnd]}, 1, 1.0

    def _front(self, hwnd: int) -> str:
        self.front = hwnd
        return ""

    def _focus(self, timeout_s: float = 0.4) -> Any:
        return SimpleNamespace(accepts_text=True, is_password=False, window=self.front, role="Edit", name="Message")

    def snapshot(self) -> None:
        return None

    def _act(self, name: str, args: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        self.calls.append((name, dict(args)))
        if name == "open_app":
            want = str(args.get("name") or "")
            hwnd = next((h for h, w in self.windows.items() if w["process_name"] == want), None)
            if hwnd is not None:
                self.front = hwnd
            facts["verified"] = hwnd is not None
            return hwnd is not None, f"opened {want}", facts
        facts["verified"] = True
        return True, "ok", facts


def el(i: int, role: str, name: str, parent: int = -1, aid: str = "") -> dict[str, Any]:
    return {"id": i, "role": role, "name": name, "parent": parent, "is_interactive": True, "automation_id": aid}


def memory_for(store: Any) -> Any:
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    return SimpleNamespace(call=lambda what, fn, timeout_s=1.0: fn(client))


def item_of(parts: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    clean = P.clean_parts(parts)
    assert clean, parts
    return {"ref": "pb:9", "id": 9, "intent": "do the usual", "apps": [], "text": "the way", "parts": clean,
            "run": [s for p in clean for s in p["run"]], "trial": False, "sends": False, **over}


# -- pick ----------------------------------------------------------------------------------------------------------

def _pick_desk(store: Any, names: list[str], *, box: str = "List") -> Desk:
    tree = [el(1, box, "Playlists")] + [el(10 + k, "ListItem", n, parent=1) for k, n in enumerate(names)]
    return Desk({100: {"title": "Tunes", "process_name": "tunes"}}, {100: tree}, 100, memory=memory_for(store))


def pick_step(store: Any, name: str | None = None, **over: Any) -> dict[str, Any]:
    step = {"do": "pick", "in": {"role": "List"}, "role": "ListItem", "app": "tunes"}
    if name is not None:
        step["item"] = TD.name_digest(store, "tunes", "ListItem", name)[:P.PICK_DIGEST_CHARS]
    step.update(over)
    return step


def test_pick_clicks_the_one_child_whose_name_digest_matches(store):
    desk = _pick_desk(store, ["Focus Beats", "Rainy Mornings", "Road Trip"])
    item = item_of([{"run": [pick_step(store, "Rainy Mornings")], "end": {"kind": "selected", "app": "tunes"}}])
    ok, report, facts = S.SkillRunner(desk).run({"steps": []}, item=item, words=["play my usual playlist"])
    assert ok and facts["verified"] is True
    assert [c for c in desk.calls if c[0] == "click"] == [("click", {"element": 11, "read": 1, "expect_hwnd": 100})]
    assert "Rainy" not in json.dumps(S.trace_steps(facts["results"], 9))   # the item's name never kept


def test_pick_with_no_match_two_matches_or_an_item_not_visible_stops_and_hands_over(store):
    step = pick_step(store, "Rainy Mornings")
    end = {"kind": "selected", "app": "tunes"}
    for names, why in ((["Focus Beats", "Road Trip"], "not visible"),            # not on the screen: not in the read
                       (["Rainy Mornings", "Rainy  mornings"], "2 items match")):
        desk = _pick_desk(store, names)
        ok, report, facts = S.SkillRunner(desk).run({"steps": []}, item=item_of([{"run": [step], "end": end}]),
                                                    words=["play my usual playlist"])
        assert not ok and facts["deviation"] == "not_found" and why in report
        assert "skill_context" in facts and not [c for c in desk.calls if c[0] == "click"]
    # no such list on the screen
    desk = _pick_desk(store, ["Rainy Mornings"], box="Tree")
    ok, report, facts = S.SkillRunner(desk).run({"steps": []}, item=item_of([{"run": [step], "end": end}]),
                                                words=["play my usual playlist"])
    assert not ok and "is not on the screen" in report


def test_pick_by_slot_matches_the_requests_words_as_a_click_matches(store):
    desk = _pick_desk(store, ["Focus Beats", "Road Trip"])
    item = item_of([{"run": [pick_step(store, slot="playlist")], "end": {"kind": "selected", "app": "tunes"}}])
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "road trip"}]}, item=item,
                                         words=["play road trip"])
    assert ok and [c[1]["element"] for c in desk.calls if c[0] == "click"] == [11]
    with pytest.raises(S.SkillRefused, match="not what they said"):
        S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Focus Beats"}]}, item=item, words=["play road trip"])


# -- parts ---------------------------------------------------------------------------------------------------------

def _two_app_desk() -> Desk:
    windows = {100: {"title": "Sheet - figures", "process_name": "sheets"},
               200: {"title": "Notes", "process_name": "notesy"}}
    trees = {100: [el(1, "Button", "Copy totals")], 200: [el(5, "Button", "Paste"), el(6, "Button", "Save")]}
    return Desk(windows, trees, 100)


def _parts() -> list[dict[str, Any]]:
    return [{"run": [{"do": "click", "role": "Button", "name": "Copy totals"}, {"do": "open_app", "name": "notesy"}],
             "end": {"kind": "window_verified", "app": "notesy"}},
            {"run": [{"do": "click", "role": "Button", "name": "Paste"}, {"do": "click", "role": "Button",
                                                                           "name": "Save"}],
             "end": {"kind": "file_saved", "app": "notesy", "ext": ".txt"}}]


def test_a_way_in_parts_runs_them_in_order_verifying_each_end(store):
    desk = _two_app_desk()
    ok, report, facts = S.SkillRunner(desk).run({"steps": []}, item=item_of(_parts()), words=["copy the totals"])
    assert ok and facts["verified"] is True and facts["parts"] == 2 and facts["steps_done"] == 4
    assert [c[0] for c in desk.calls] == ["click", "open_app", "click", "click"]


def test_a_part_whose_end_is_not_reached_stops_the_run_and_hands_over(store):
    desk = _two_app_desk()
    desk.windows[200]["process_name"] = "otherapp"            # the app opens, but it is not the one the part ends in
    desk.windows[300] = {"title": "Notes", "process_name": "notesy"}
    desk.trees[300] = desk.trees[200]
    desk._act = lambda name, args, facts: (True, "opened", {**facts, "verified": True})   # nothing comes to the front
    ok, report, facts = S.SkillRunner(desk).run({"steps": []}, item=item_of(_parts()), words=["copy the totals"])
    assert not ok and facts["deviation"] == "part_end" and facts["verified"] is False
    assert "part 1 of the way did not reach its checked end" in report
    assert "skill_context" in facts and "do not redo" in facts["skill_context"].lower()
    assert [c[0] for c in desk.calls] == ["click"]            # nothing of part 2 ran (open_app went through _act)


def test_parts_take_the_talkers_text_only_for_typing_and_the_rest_from_memory(store):
    desk = Desk({100: {"title": "Chat", "process_name": "chatter"}}, {100: [el(1, "Button", "New message")]}, 100)
    parts = [{"run": [{"do": "click", "role": "Button", "name": "New message"}, {"do": "type", "slot": "message"},
                      {"do": "keys", "keys": ["enter"]}], "end": {"kind": "field_submitted", "app": "chatter"}}]
    item = item_of(parts, sends=True)
    words = ["send hello there to Mira"]
    with pytest.raises(S.SkillRefused, match="their own words"):   # a demo never authorises a send
        S.SkillRunner(desk).run({"steps": [{"do": "type", "text": "hello there"}], "sends": False}, item=item,
                                words=words)
    with pytest.raises(S.SkillRefused, match="not what they said"):
        S.SkillRunner(desk).run({"steps": [{"do": "type", "text": "hello there"}], "sends": True, "to": "Riya",
                                 "what": "hello there"}, item=item, words=words)
    ok, _r, facts = S.SkillRunner(desk).run({"steps": [{"do": "type", "text": "hello there"}], "sends": True,
                                             "to": "Mira", "what": "hello there"}, item=item, words=words)
    assert ok and [c[0] for c in desk.calls] == ["click", "type_text", "hotkey"]
    assert desk.calls[1][1]["text"] == "hello there"
    with pytest.raises(S.SkillRefused):                        # an invented step is not the way's
        S.SkillRunner(desk).run({"steps": [{"do": "click", "name": "Delete"}], "sends": True, "to": "Mira",
                                 "what": "hello there"}, item=item, words=words)


def test_more_than_4_parts_or_a_trial_or_a_command_is_refused_before_anything_runs():
    desk = Desk({}, {}, 0)
    step = {"do": "keys", "keys": ["enter"]}
    end = {"kind": "window_verified", "app": "x"}
    five = {"ref": "pb:1", "parts": [{"run": [step], "end": end}] * 5, "run": [step] * 5}
    with pytest.raises(S.SkillRefused, match="more than 4 parts"):
        S.SkillRunner(desk).run({"steps": []}, item=five, words=["x"])
    with pytest.raises(S.SkillRefused, match="seen only once"):
        S.SkillRunner(desk).run({"steps": [step]}, item={"ref": "pb:2", "run": [step], "trial": True}, words=["x"])
    cmd = {"ref": "pb:3", "parts": [{"run": [{"do": "command", "slot": "build"}], "end": {"kind": "command_ok"}}]}
    with pytest.raises(S.SkillRefused, match="terminal command"):
        S.SkillRunner(desk).run({"steps": []}, item=cmd, words=["build it"])
    assert desk.calls == []
    assert "command" not in S.SKILL_DOS and "pick" in S.SKILL_DOS


def test_a_proven_automation_id_finds_the_control_first(store):
    desk = Desk({100: {"title": "Editor", "process_name": "editr"}},
                {100: [el(1, "Button", "Save", aid="Other"), el(2, "Button", "Save", aid="SaveBtn")]}, 100)
    item = {"ref": "pb:5", "run": [{"do": "click", "role": "Button", "name": "Save", "aid": "SaveBtn"}]}
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "click", "role": "Button", "name": "Save"}]}, item=item,
                                         words=["save it"])
    assert ok and desk.calls[0] == ("click", {"element": 2, "read": 1, "expect_hwnd": 100})


# -- caps unchanged ------------------------------------------------------------------------------------------------

def test_caps_unchanged():
    from yuki.agent import frame
    from yuki.voice import practice as VP

    assert S.MAX_STEPS == 8 and P.MAX_RUN_STEPS == 8 and P.MAX_PARTS == S.MAX_PARTS == 4
    assert frame.FRAME_CHARS == 2_500 and VP.SKILL_CHARS == 520 and VP.LESSONS_CHARS == 400
    assert VP.SKILL_MIN_CONFIDENCE == 0.70 and P.MAX_WAYS == 2
    from yuki.agent.tools import tool_params

    assert len(json.dumps(tool_params(cacheable=False, screenshot_policy="never"))) < 39_000
    assert len(json.dumps(S.RUN_SKILL_TOOL)) < 1_500


def test_the_frame_keeps_its_cap_with_a_trial_a_path_and_pitfalls():
    from yuki.agent import frame

    practice = {"ways": [{"ref": "pb:1", "id": 1, "kind": "procedure", "intent": "send the weekly report",
                          "apps": ["chatter"], "label": P.TRIAL_LABEL, "text": "1. Open chatter " * 40,
                          "trial": True}],
                "pitfalls": [{"ref": "pb:2", "id": 2, "intent": "pasted names", "apps": ["chatter"],
                              "label": "active", "text": "A pasted name stays text. " * 20}],
                "path": {"family": "You sent the weekly report", "tasks": 3, "steps": ["in chatter: press Ctrl+K"] * 8},
                "example": None, "go_tos": []}
    built = frame.build({}, practice=practice)
    assert built.error is None and 0 < len(built.text) <= frame.FRAME_CHARS
    assert P.TRIAL_LABEL in built.text and "[pb:2]" in built.text       # the trial shown as a hint, the pitfall whole


# -- review fixes ---------------------------------------------------------------------------------------------------

def test_a_demo_way_with_no_runnable_parts_is_never_run_and_never_offered(store):
    """Review F2: a way learned from what the user did that Yuki's hands cannot take (no parts) is a hint for the
    thinker even once it is a way: the talker is not offered it and the skill runner refuses the talker's own steps."""
    from yuki.voice.practice import qualifies

    desk = Desk({100: {"title": "Chat", "process_name": "chatter"}}, {100: []}, 100)
    item = {"ref": "pb:2", "id": 2, "kind": "procedure", "intent": "send a message", "apps": ["chatter"], "run": [],
            "parts": [], "demo": True, "sends": True, "hint_only": True, "trial": False, "state": "active",
            "confidence": 0.8, "cosine": 0.95}
    assert not qualifies(item)
    args = {"steps": [{"do": "type", "text": "hello there"}, {"do": "keys", "keys": ["enter"]}], "sends": False}
    with pytest.raises(S.SkillRefused, match="hint for the thinker"):
        S.SkillRunner(desk).run(args, item=item, words=["send hello there"])
    with pytest.raises(S.SkillRefused, match="hint for the thinker"):   # the flag lost: a demo with no parts still
        S.SkillRunner(desk).run(args, item={**item, "hint_only": False}, words=["send hello there"])
    assert desk.calls == []
    assert qualifies({**item, "hint_only": False}) and not qualifies({**item, "hint_only": False, "trial": True})


def test_a_way_in_parts_with_typing_and_no_steps_given_runs_nothing_and_says_so(store):
    """Review F4: the talker leaving out a way's typing steps is refused, never a run of 0 steps reported done."""
    desk = Desk({100: {"title": "Chat", "process_name": "chatter"}}, {100: [el(1, "Button", "New message")]}, 100)
    parts = [{"run": [{"do": "open_app", "name": "chatter"}, {"do": "type", "slot": "message"},
                      {"do": "keys", "keys": ["enter"]}], "end": {"kind": "field_submitted", "app": "chatter"}}]
    with pytest.raises(S.SkillRefused, match="no steps to run"):
        S.SkillRunner(desk).run({"steps": [], "sends": True, "to": "Mira", "what": "hi"}, item=item_of(parts),
                                words=["send hi to Mira"])
    assert desk.calls == []


def test_a_part_before_the_last_ends_only_on_a_state_checked_on_the_screen(store):
    """Review F7: a fact recorded between two steps (a control selected) cannot be checked when the part runs, so it
    never ends a part before the last: practice memory drops such parts and the runner refuses them."""
    click = {"do": "click", "role": "Button", "name": "Go"}
    mid_fact = [{"run": [click], "end": {"kind": "selected", "app": "x"}},
                {"run": [click], "end": {"kind": "window_verified", "app": "x"}}]
    assert P.clean_parts(mid_fact) == []
    assert P.clean_parts([{"run": [click], "end": {"kind": "window_verified", "app": "x"}},
                          {"run": [click], "end": {"kind": "selected", "app": "x"}}])   # the last may
    desk = Desk({100: {"title": "X", "process_name": "x"}}, {100: [el(1, "Button", "Go")]}, 100)
    raw = {"ref": "pb:7", "parts": mid_fact, "run": [click, click]}
    with pytest.raises(S.SkillRefused, match="cannot be checked on the screen"):
        S.SkillRunner(desk).run({"steps": []}, item=raw, words=["go"])
    assert desk.calls == []
    # and the trace's facts between two steps are no cut point
    rows = [{"id": 1, "at": 1.0, "source": "user", "resolved": "point", "process": "x", "role": "ListItem",
             "facts": json.dumps({"selected": True})},
            {"id": 2, "at": 2.0, "source": "user", "resolved": "event", "process": "x",
             "facts": json.dumps({"window_verified": "x"})}]
    assert TD.state_between(rows, {}, 0.0, 1.5) is None
    assert TD.state_between(rows, {}, 0.0, 3.0)["kind"] == "window_verified"
    assert TD.state_between(rows, {}, 0.0, 1.5, kinds=P.END_KINDS)["kind"] == "selected"


# -- second verification fixes ----------------------------------------------------------------------------------------

def _send_desk(store: Any, judged: list[Any]) -> Desk:
    tree = [el(1, "List", "Chats", aid="chats"), el(11, "ListItem", "Bob Stone", parent=1),
            el(12, "ListItem", "Alice Park", parent=1)]
    desk = Desk({100: {"title": "Chatter", "process_name": "chatter"}}, {100: tree}, 100, memory=memory_for(store))
    desk.authorize = lambda tool, tool_input, target, earlier, force=False: judged.append((tool, dict(tool_input)))
    return desk


def test_a_way_that_sends_never_picks_a_fixed_item_and_a_named_pick_is_the_recipient(store):
    """Second verification V1: a send goes to whom the user's words name - never the chat the demo picked - and Yuki's
    own check sees whose item it picks."""
    judged: list[Any] = []
    desk = _send_desk(store, judged)
    end = {"kind": "field_submitted", "app": "chatter"}
    bob = {"do": "pick", "in": {"role": "List"}, "role": "ListItem", "app": "chatter",
           "item": TD.name_digest(store, "chatter", "ListItem", "Bob Stone")[:P.PICK_DIGEST_CHARS]}
    tail = [{"do": "type", "slot": "message"}, {"do": "keys", "keys": ["enter"]}]
    fixed = item_of([{"run": [bob, *tail], "end": end}], sends=True, demo=True)
    with pytest.raises(S.SkillRefused, match="always picks the same item"):
        S.SkillRunner(desk).run({"steps": [{"do": "type", "text": "hi there"}], "sends": True, "to": "Alice",
                                 "what": "hi there"}, item=fixed, words=["send hi there to Alice"])
    slot = {k: v for k, v in bob.items() if k != "item"} | {"slot": "person or chat"}
    named = item_of([{"run": [slot, *tail], "end": end}], sends=True, demo=True)
    with pytest.raises(S.SkillRefused, match="the message goes to"):
        S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Alice Park"}, {"do": "type", "text": "hi there"}],
                                 "sends": True, "to": "Alice", "what": "hi there"}, item=named,
                                words=["send hi there to Alice", "Alice Park"])
    assert desk.calls == []
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "pick", "text": "Alice Park"},
                                                    {"do": "type", "text": "hi there"}],
                                          "sends": True, "to": "Alice Park", "what": "hi there"}, item=named,
                                         words=["send hi there to Alice Park"])
    assert ok and [c[1]["element"] for c in desk.calls if c[0] == "click"] == [12]
    assert any(tool == "click" and "Alice Park" in str(inp.get("element")) for tool, inp in judged)


def test_a_proven_automation_id_never_clicks_a_control_with_another_name(store):
    """Second verification V2: an id shared by two controls is no identity: the name must agree too."""
    desk = Desk({100: {"title": "Editor", "process_name": "editr"}}, {100: [el(7, "Button", "Delete", aid="btn")]}, 100)
    item = {"ref": "pb:5", "run": [{"do": "click", "role": "Button", "name": "Save", "aid": "btn"}]}
    ok, report, facts = S.SkillRunner(desk).run({"steps": [{"do": "click", "role": "Button", "name": "Save"}]},
                                                item=item, words=["save it"])
    assert not ok and facts["deviation"] == "not_found" and desk.calls == []
    # a control with no name of its own, the only one with that id: the id finds it
    desk = Desk({100: {"title": "Editor", "process_name": "editr"}}, {100: [el(8, "Button", "", aid="SaveBtn")]}, 100)
    item = {"ref": "pb:5", "run": [{"do": "click", "role": "Button", "name": "Save", "aid": "SaveBtn"}]}
    ok, _r, _f = S.SkillRunner(desk).run({"steps": [{"do": "click", "role": "Button", "name": "Save"}]}, item=item,
                                         words=["save it"])
    assert ok and desk.calls[0][1]["element"] == 8


def test_a_new_page_is_the_end_apps_own_window_changing(store):
    """Second verification V7: a part ending on a new page passes only when the end app's own window changed by the
    part's last step - not because another app was in front when the part began."""
    def desk() -> Desk:
        windows = {100: {"title": "Chat - general", "process_name": "chatter"},
                   200: {"title": "Old page - Webby", "process_name": "webby"}}
        return Desk(windows, {100: [], 200: [el(5, "Hyperlink", "Docs")]}, 100)

    parts = [{"run": [{"do": "open_app", "name": "webby"}, {"do": "click", "role": "Hyperlink", "name": "Docs"}],
              "end": {"kind": "page_reached", "app": "webby"}}]
    still = desk()
    ok, report, facts = S.SkillRunner(still).run({"steps": []}, item=item_of(parts), words=["open the docs"])
    assert not ok and facts["deviation"] == "part_end" and "no new page came up" in report
    moved = desk()
    moved.effects[5] = lambda: moved.windows[200].update(title="Docs - Webby")
    ok, _r, facts = S.SkillRunner(moved).run({"steps": []}, item=item_of(parts), words=["open the docs"])
    assert ok and facts["verified"] is True


def test_a_pick_finds_its_item_only_in_its_own_list(store):
    """Second verification V8: the container's identity (C1's keyed digest) is matched live; without it the item must
    be the only match in the whole window."""
    def desk(*items: tuple[int, int]) -> Desk:
        tree = [el(1, "List", "Chats", aid="chats"), el(2, "List", "Contacts", aid="contacts")]
        tree += [el(i, "ListItem", "Bob Stone", parent=p) for i, p in items]
        return Desk({100: {"title": "Chatter", "process_name": "chatter"}}, {100: tree}, 100, memory=memory_for(store))

    box = TD.container_digest(store, "chatter", "List", "chats", "")[:P.PICK_DIGEST_CHARS]
    step = {"do": "pick", "in": {"role": "List", "digest": box}, "role": "ListItem", "app": "chatter",
            "item": TD.name_digest(store, "chatter", "ListItem", "Bob Stone")[:P.PICK_DIGEST_CHARS]}
    end = {"kind": "selected", "app": "chatter"}
    item = item_of([{"run": [step], "end": end}])
    assert item["parts"][0]["run"][0]["in"]["digest"] == box           # practice memory keeps the container's digest
    only_contacts = desk((21, 2))
    ok, _r, facts = S.SkillRunner(only_contacts).run({"steps": []}, item=item, words=["open bob"])
    assert not ok and facts["deviation"] == "not_found" and not only_contacts.calls
    both = desk((11, 1), (21, 2))
    ok, _r, _f = S.SkillRunner(both).run({"steps": []}, item=item, words=["open bob"])
    assert ok and [c[1]["element"] for c in both.calls if c[0] == "click"] == [11]
    # no container identity kept: a same-named item in another list makes it unclear
    loose = item_of([{"run": [{**step, "in": {"role": "List"}}], "end": end}])
    unclear = desk((11, 1), (21, 2))
    ok, report, _f = S.SkillRunner(unclear).run({"steps": []}, item=loose, words=["open bob"])
    assert not ok and "2 items match" in report and not unclear.calls


def test_a_demo_ways_typing_is_only_ever_the_users_words(store):
    """Second verification V9: a way learned from what the user did types only their words, never its own step
    words (decision D8)."""
    desk = Desk({100: {"title": "Chat", "process_name": "chatter"}}, {100: [el(1, "Button", "Attach")]}, 100)
    parts = [{"run": [{"do": "click", "role": "Button", "name": "Attach"}, {"do": "type", "slot": "message"}],
              "end": {"kind": "field_submitted", "app": "chatter"}}]
    item = item_of(parts, demo=True, text='1. Click Button "Attach" in chatter 2. Type the message in chatter')
    with pytest.raises(S.SkillRefused, match="neither their words"):
        S.SkillRunner(desk).run({"steps": [{"do": "type", "text": "Attach in chatter"}]}, item=item,
                                words=["do the usual"])
    assert desk.calls == []
