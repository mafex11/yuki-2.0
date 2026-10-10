"""sWeb: the `message` and `discord.voice` skills (PLAN.md S-2; D0, D17, D19, rule 8; B.2; map-skills S2.5).

Invented people and chats only (``_sweb_world``); a temp database; a stand-in desk for the voice hands, so the real step
runner runs the quick switcher and the typing against it. The drivers' live plumbing (focus, opening a link, the Now
board, the microphone, a button press) is replaced per test, and the real one is made unreachable: nothing here
touches the desktop, a window, the microphone or the real memory.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace
from typing import Any

import pytest

from _sweb_world import NOW, SECRETS, Clock, Desk, board_entry, msg, open_store, world
from yuki.memory.api import MemoryClient
from yuki.skills import chat as CH
from yuki.skills import connection as C
from yuki.skills import donecheck as D
from yuki.skills import engine as E
from yuki.skills import registry as R
from yuki.skills import uia_route as U
from yuki.skills.resolve import Live, resolve

DISCORD_HWND, WA_HWND, SLACK_HWND = 500, 600, 700


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("a test reached the real desktop")


@pytest.fixture(autouse=True)
def guard(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The real desktop is unreachable; the drivers' plumbing is a recorder. ``state`` drives it."""
    from yuki.actions import launch, tabs
    from yuki.memory import gamegate
    from yuki.perception import windows

    for mod, name in ((launch, "focus_window"), (launch, "open_url"), (launch, "launch_app"), (tabs, "tab_action"),
                      (U, "press"), (U, "app_window"), (windows, "list_windows")):
        monkeypatch.setattr(mod, name, _boom)
    monkeypatch.setattr(U, "_uia", _boom)
    state: dict[str, Any] = {"game": "", "board": None, "opened": [], "focused": [], "refreshed": [],
                             "hwnds": {"Discord.exe": DISCORD_HWND, "WhatsApp.Root.exe": WA_HWND,
                                       "slack.exe": SLACK_HWND},
                             "mic": [False], "pressed": [], "refusals": []}
    monkeypatch.setattr(gamegate, "game_in_front", lambda: state["game"])

    def refused(action: str, hwnd: int | None = None) -> bool:
        if state["game"]:
            state["refusals"].append(action)
        return bool(state["game"])

    monkeypatch.setattr(CH, "_refused", refused)
    monkeypatch.setattr(CH, "_board_now", lambda wait_s=0.15: state["board"])
    monkeypatch.setattr(CH, "_board_refresh", lambda hwnd: state["refreshed"].append(hwnd) or True)
    monkeypatch.setattr(CH, "_app_hwnd", lambda data: next((state["hwnds"].get(i, 0) for i in data.get("images") or []
                                                            if state["hwnds"].get(i)), 0))

    def focus(hwnd: int) -> Any:
        state["focused"].append(hwnd)
        return SimpleNamespace(ok=True, details={})

    def open_uri(uri: str) -> Any:
        state["opened"].append(uri)
        if state.get("on_open"):
            state["on_open"](uri)
        return SimpleNamespace(ok=True, details={})

    monkeypatch.setattr(CH, "_focus", focus)
    monkeypatch.setattr(CH, "_open_uri", open_uri)

    def mic_held_by(want: str) -> tuple[bool | None, int]:
        seq = state["mic"]
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        return v, 1

    monkeypatch.setattr(D, "_mic_held_by", mic_held_by)
    monkeypatch.setattr(CH, "_press", lambda hwnd, role, name: state["pressed"].append((hwnd, role, name)) or
                        state.get("press_outcome", "pressed"))
    return state


@pytest.fixture()
def client(tmp_path):
    C.clear_cache()
    store = open_store(tmp_path)
    c = MemoryClient(store)
    c.w = world(store)
    R.seed(store, C.load())
    yield c
    c.close()


def _desk(client: Any, state: dict[str, Any], hwnd: int = DISCORD_HWND, process: str = "Discord.exe") -> Desk:
    desk = Desk({DISCORD_HWND: {"title": "Discord", "process_name": "Discord.exe"},
                 WA_HWND: {"title": "WhatsApp", "process_name": "WhatsApp.Root.exe"},
                 SLACK_HWND: {"title": "Slack", "process_name": "slack.exe"}}, hwnd,
                memory=SimpleNamespace(call=lambda what, fn, timeout_s=1.0: fn(client)))
    return desk


def _ctx(client: Any, *, hands: Any = None, words: list[str], origin: str = "voice", log: list | None = None,
         game: Any = None) -> E.RunContext:
    logs = log if log is not None else []
    clock = Clock()
    return E.RunContext(origin=origin, words=words, memory=client, hands=hands, recheck="inline",
                        game=game or (lambda: ""), log=lambda event, **f: logs.append((event, f)),
                        clock=clock.now, sleep=clock.sleep)


def _open_on(state: dict[str, Any], client: Any, tid: int, hwnd: int, process: str) -> Any:
    """An effect: the window now shows the chat ``tid`` (the Now board's fresh read of it)."""
    def effect(*_a: Any) -> None:
        state["board"] = {"windows": [board_entry(client.store, tid, hwnd=hwnd, read_at=NOW + 10 ** 6,
                                                  process=process)]}
    return effect


def _run(client: Any, ctx: E.RunContext, slots: dict[str, Any], key: str = "message") -> E.SkillResult:
    return E.resolve_and_run(key, slots, ctx.words, ctx, live=Live())


def _runs(client: Any, key: str = "message") -> list[dict]:
    return [dict(r) for r in client.store._query("SELECT * FROM skill_runs WHERE skill = ? ORDER BY id", (key,))]


# -- whom and where ---------------------------------------------------------------------------------------------------


def test_the_person_their_usual_app_and_a_named_app(client) -> None:
    t = CH.find_target(client.store, "Vesper")
    assert isinstance(t, CH.Target) and t.name == "Vesper Lark" and [r.app for r in t.routes] == ["discord", "whatsapp"]
    assert CH.pick_route(t, None).app == "discord"                      # the busy DM leads
    assert CH.pick_route(t, "whatsapp").app == "whatsapp"
    orrin = CH.find_target(client.store, "Orrin")
    ask = CH.pick_route(orrin, "whatsapp")
    assert isinstance(ask, CH.Asked) and ask.line == "I don't have a WhatsApp chat with Orrin Tal. Slack instead?"
    two = CH.find_target(client.store, "Mira")
    assert isinstance(two, CH.Asked) and "Mira Dune" in two.line and "Mira Solt" in two.line
    place = CH.find_target(client.store, "lantern-club")
    assert isinstance(place, CH.Target) and place.place and place.routes[0].app == "discord"
    assert place.routes[0].kind == "channel"
    nobody = CH.find_target(client.store, "Zeb Quorn")
    assert isinstance(nobody, CH.Asked) and nobody.line == "I don't know who Zeb Quorn is."


def test_deep_links_come_from_the_chats_own_address_by_the_apps_shapes(client) -> None:
    v = CH.find_target(client.store, "Vesper")
    dc, wa = v.routes
    assert CH.deep_link(CH.app_data("discord"), dc) == "discord://-/channels/@me/111122223333"
    assert CH.deep_link(CH.app_data("whatsapp"), wa) == "whatsapp://send?phone=447700900123"
    club = CH.find_target(client.store, "lantern-club").routes[0]
    assert CH.deep_link(CH.app_data("discord"), club) == "discord://-/channels/900000000000000001/900000000000000002"
    orrin = CH.find_target(client.store, "Orrin").routes[0]
    assert CH.deep_link(CH.app_data("slack"), orrin) == "slack://channel?team=T0INVENT1&id=D0INVENT2"
    juno = CH.find_target(client.store, "Juno").routes[0]
    assert CH.deep_link(CH.app_data("discord"), juno) is None          # no address: the quick switcher
    thistle = CH.find_target(client.store, "Thistle").routes[0]
    assert CH.deep_link(CH.app_data("whatsapp"), thistle) is None      # no phone handle: the search route
    # a handle that is not a phone number (letters, too short) never makes a phone link
    for handle in ("vesper.lark", "12345", "+12 abc 3456789"):
        assert CH._phone(handle) is None


def test_the_switcher_query_uses_the_apps_filter_without_doubling_it(client) -> None:
    data = CH.app_data("discord")
    juno = CH.find_target(client.store, "Juno").routes[0]
    assert CH.switcher_query(data, juno) == "@juno.pike"
    club = CH.find_target(client.store, "lantern-club").routes[0]
    assert CH.switcher_query(data, club) == "#lantern-club"


# -- opening a chat ---------------------------------------------------------------------------------------------------


def test_message_someone_by_the_quick_switcher_opens_the_chat_checked_by_thread_id(client, guard) -> None:
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")
    logs: list = []
    got = _run(client, _ctx(client, hands=desk, words=["message Juno"], log=logs), {"person": "Juno"})
    assert got.outcome == "done" and got.verified is True, got
    assert got.done_line == "Your chat with Juno Pike is open on Discord."
    assert desk.keys() == ["ctrl+k", "enter"] and desk.typed() == ["@juno.pike"]
    assert guard["focused"] == [DISCORD_HWND] and guard["opened"] == []
    assert {t for t, _i in desk.judged} >= {"hotkey", "type_text"}          # Yuki's own check saw each key and text
    assert got.route == "A:discord_switch:driver"
    _no_secrets(logs, client)


def test_a_draft_types_their_words_and_never_presses_send(client, guard) -> None:
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")
    got = _run(client, _ctx(client, hands=desk, words=["message Juno see you at nine"]),
               {"person": "Juno", "text": "see you at nine"})
    assert got.outcome == "done" and got.done_line == "It's typed in your chat with Juno Pike on Discord, not sent."
    assert desk.typed() == ["@juno.pike", "see you at nine"] and desk.keys() == ["ctrl+k", "enter"]


def test_send_x_to_y_presses_send_and_is_done_only_when_memory_captured_it(client, guard) -> None:
    desk = _desk(client, guard)
    tid = client.w["juno_dc"]
    presses = {"n": 0}

    def enter() -> None:
        presses["n"] += 1
        if presses["n"] == 1:
            _open_on(guard, client, tid, DISCORD_HWND, "Discord.exe")()
        else:                                   # the watcher captures the sent message in that chat
            msg(client.store, tid, 999, me=True, at=NOW + 5, text="see you at nine")
            client.store._write(lambda conn: conn.execute("UPDATE messages SET first_seen = ? WHERE thread_id = ? "
                                                          "AND text_ciphertext IS NOT NULL AND id = (SELECT max(id) "
                                                          "FROM messages)", (10 ** 10, tid)))

    desk.effects[("hotkey", "enter")] = enter
    words = ["send see you at nine to Juno"]
    got = _run(client, _ctx(client, hands=desk, words=words),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "done" and got.done_line == "Sent to Juno Pike on Discord.", got
    assert desk.keys() == ["ctrl+k", "enter", "enter"]
    assert desk.typed() == ["@juno.pike", "see you at nine"]
    rows = _runs(client)
    assert rows[-1]["outcome"] == "done" and rows[-1]["checked"] == 1


def test_a_send_not_seen_landing_is_said_plainly_and_never_tried_twice(client, guard) -> None:
    desk = _desk(client, guard)
    presses = {"n": 0}

    def enter() -> None:
        presses["n"] += 1
        if presses["n"] == 1:
            _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")()

    desk.effects[("hotkey", "enter")] = enter
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "unverified" and got.verified is None and not got.done
    assert got.say == "I pressed send to Juno Pike, but couldn't confirm it landed."
    assert desk.keys() == ["ctrl+k", "enter", "enter"]                      # one acting rung only
    assert _runs(client)[-1]["outcome"] == "unverified" and _runs(client)[-1]["checked"] is None


def test_nothing_is_typed_into_a_chat_that_is_not_confirmed_open(client, guard) -> None:
    desk = _desk(client, guard)                 # the board never shows the chat; its thread is not read since
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "unverified" and not got.done
    assert got.say == "I opened Discord, but couldn't confirm it's your chat with Juno Pike, so I didn't type anything."
    assert desk.typed() == ["@juno.pike"] and desk.keys() == ["ctrl+k", "enter"]   # the text never typed, no send
    assert guard["refreshed"] == [DISCORD_HWND]                          # the watcher was asked to read it again


def test_another_chat_in_front_is_a_no_and_a_send_stops_there(client, guard) -> None:
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["dune_dc"], DISCORD_HWND, "Discord.exe")
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    # review r2 L4: nothing was typed, so the driver's own line is said (never "the PC shows it didn't go through")
    # and the run is settled, not a failed send; no second rung, no hand-off
    assert got.outcome == "asked" and not got.done and got.handoff is None
    assert got.say == "I opened Discord, but couldn't confirm it's your chat with Juno Pike, so I didn't type anything."
    assert "didn't go through" not in got.say
    assert desk.typed() == ["@juno.pike"] and desk.keys() == ["ctrl+k", "enter"]   # never typed into the wrong chat
    row = _runs(client)[-1]
    assert row["outcome"] == "asked" and row["checked"] is None


def test_a_deep_link_opens_the_chat_with_no_key_and_works_without_the_voice_hands(client, guard) -> None:
    guard["on_open"] = lambda uri: _open_on(guard, client, client.w["vesper_dc"], DISCORD_HWND, "Discord.exe")()
    got = _run(client, _ctx(client, hands=None, words=["open my chat with Vesper"], origin="thinker"),
               {"person": "Vesper"})
    assert got.outcome == "done" and got.route == "A:discord_link:driver"
    assert guard["opened"] == ["discord://-/channels/@me/111122223333"] and guard["focused"] == []


def test_typing_needs_the_voice_hands_otherwise_the_thinker_gets_it_pinned(client, guard) -> None:
    got = _run(client, _ctx(client, hands=None, words=["message Vesper otter cove"], origin="thinker"),
               {"person": "Vesper", "text": "otter cove"})
    assert got.outcome == "handed_over" and got.handoff and got.handoff["key"] == "message"
    assert guard["opened"] == [] and guard["focused"] == []
    assert got.handoff["done"]["probe"] == "driver" and got.handoff["done"]["call"] == "message_done"


def test_whatsapp_by_its_phone_link_and_slack_by_its_deep_link(client, guard) -> None:
    guard["on_open"] = lambda uri: _open_on(guard, client, client.w["vesper_wa"], WA_HWND, "WhatsApp.Root.exe")()
    got = _run(client, _ctx(client, words=["message Vesper on WhatsApp"]), {"person": "Vesper", "app": "WhatsApp"})
    assert got.outcome == "done" and guard["opened"] == ["whatsapp://send?phone=447700900123"]
    assert got.done_line == "Your chat with Vesper Lark is open."              # the app they named isn't repeated
    guard["opened"].clear()
    guard["on_open"] = lambda uri: _open_on(guard, client, client.w["orrin_sl"], SLACK_HWND, "slack.exe")()
    got = _run(client, _ctx(client, words=["message Orrin"]), {"person": "Orrin"})
    assert got.outcome == "done" and guard["opened"] == ["slack://channel?team=T0INVENT1&id=D0INVENT2"]
    assert got.done_line == "Your chat with Orrin Tal is open on Slack."


def test_whatsapp_search_picks_the_row_by_the_digest_of_the_chats_name(client, guard) -> None:
    desk = _desk(client, guard, WA_HWND, "WhatsApp.Root.exe")
    desk.front = WA_HWND
    tree = [{"id": 1, "role": "List", "name": "Search results", "parent": -1, "is_interactive": True},
            {"id": 11, "role": "ListItem", "name": "Thistle Hollow", "parent": 1, "is_interactive": True},
            {"id": 12, "role": "ListItem", "name": "Vesper Lark", "parent": 1, "is_interactive": True}]
    desk._read_tree = lambda hwnd: ({"elements": tree}, 1, 1.0)
    desk.effects[11] = _open_on(guard, client, client.w["thistle_wa"], WA_HWND, "WhatsApp.Root.exe")
    got = _run(client, _ctx(client, hands=desk, words=["message Thistle otter cove"]),
               {"person": "Thistle", "text": "otter cove"})
    assert got.outcome == "done" and got.route == "A:whatsapp_search:driver", got
    assert desk.keys() == ["ctrl+f"] and desk.typed() == ["Thistle Hollow", "otter cove"]
    assert [a["element"] for n, a in desk.calls if n == "click"] == [11]


def test_two_people_or_nobody_is_a_question_never_a_guess(client, guard) -> None:
    desk = _desk(client, guard)
    got = _run(client, _ctx(client, hands=desk, words=["message Mira"]), {"person": "Mira"})
    assert got.outcome == "asked" and "Mira Dune" in got.ask and desk.calls == []
    got = _run(client, _ctx(client, hands=desk, words=["message Zeb Quorn"]), {"person": "Zeb Quorn"})
    assert got.outcome == "asked" and got.ask == "I don't know who Zeb Quorn is." and desk.calls == []
    got = _run(client, _ctx(client, hands=desk, words=["message Orrin on WhatsApp"]),
               {"person": "Orrin", "app": "WhatsApp"})
    assert got.outcome == "asked" and got.options == ["slack"] and desk.calls == []


def test_rule_8_text_not_in_their_words_is_never_typed_and_a_held_check_asks(client, guard) -> None:
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")
    # the talker's invented text is dropped by the resolver (not a span of the words): the chat only opens
    got = _run(client, _ctx(client, hands=desk, words=["message Juno"]),
               {"person": "Juno", "text": "an invented reply", "send": "send"})
    assert got.outcome == "done" and "an invented reply" not in desk.typed()
    # Yuki's own check holds the typing: asked, nothing typed after it
    desk2 = _desk(client, guard)
    desk2.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")
    desk2.hold["type_text"] = SimpleNamespace(allowed=False, question="Send that to Juno?", why="not plain")
    got = _run(client, _ctx(client, hands=desk2, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "asked" and desk2.typed() == [] and desk2.keys() == ["ctrl+k"]


def test_a_game_in_front_holds_every_message_route_with_no_input_at_all(client, guard) -> None:
    guard["game"] = "skyrunner"
    desk = _desk(client, guard)
    for slots in ({"person": "Juno"}, {"person": "Vesper", "text": "otter cove", "send": "send"},
                  {"person": "Orrin"}):
        got = _run(client, _ctx(client, hands=desk, words=["message Juno Vesper Orrin otter cove send"],
                                game=lambda: "skyrunner"), slots)
        assert got.outcome == "held_game" and got.say == "You're in a game: after the round?"
    assert desk.calls == [] and guard["opened"] == [] and guard["focused"] == []


def test_the_app_not_running_passes_to_the_thinker(client, guard) -> None:
    guard["hwnds"] = {}
    got = _run(client, _ctx(client, hands=_desk(client, guard), words=["message Juno"]), {"person": "Juno"})
    assert got.outcome == "handed_over" and guard["focused"] == []


def test_the_done_check_after_a_hand_off_reads_memory_not_the_route(client, guard) -> None:
    tid = client.w["juno_dc"]
    since = NOW + 100
    ctx = D.ProbeContext(since=since, memory_call=lambda what, fn: fn(client))
    args = {"app": "discord", "person": "Juno", "text": "", "send": "draft"}
    assert CH.probe_message_done(ctx, args)[0] is None                    # nothing read since
    guard["board"] = {"windows": [board_entry(client.store, tid, hwnd=DISCORD_HWND, read_at=since + 1,
                                              process="Discord.exe")]}
    assert CH.probe_message_done(ctx, args)[0] is True
    guard["board"] = {"windows": [board_entry(client.store, client.w["dune_dc"], hwnd=DISCORD_HWND,
                                              read_at=since + 1, process="Discord.exe")]}
    assert CH.probe_message_done(ctx, args)[0] is False                   # another chat of that app in front
    send = {**args, "text": "otter cove", "send": "send"}
    assert CH.probe_message_done(ctx, send)[0] is None                    # not captured yet: never a "no"
    msg(client.store, tid, 4242, me=True, at=since + 2, text="otter cove")
    client.store._write(lambda conn: conn.execute("UPDATE messages SET first_seen = ? WHERE id = (SELECT max(id) "
                                                  "FROM messages)", (since + 2,)))
    assert CH.probe_message_done(ctx, send)[0] is True


def test_done_is_said_only_on_a_seen_result_over_random_chat_states(client, guard, monkeypatch) -> None:
    """Property (D19): over random chat-open answers and captures, "done" is said only when the done-check saw it."""
    rng = random.Random(7)
    seen: list[tuple[bool | None, bool]] = []
    real = CH.probe_message_done

    def probe(ctx: Any, args: Any) -> Any:
        got = real(ctx, args)
        seen.append((got[0], bool(args.get("live"))))
        return got

    monkeypatch.setitem(CH.PROBES, "message_done", probe)
    for trial in range(40):
        states = [rng.choice([True, False, None]) for _ in range(30)]
        monkeypatch.setattr(CH, "chat_state", lambda *a, **k: (states.pop(0) if states else None, {}))
        desk = _desk(client, guard)
        seen.clear()
        mode = rng.choice(["open", "draft", "send"])
        slots = {"person": "Juno"} if mode == "open" else {"person": "Juno", "text": "see you at nine",
                                                           "send": "send" if mode == "send" else "draft"}
        got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]), slots)
        done_reads = [v for v, live in seen if not live]
        if got.done:
            assert done_reads and done_reads[-1] is True, (trial, got)
        if mode == "send" and got.done:
            assert desk.keys()[-1] == "enter"
    rows = _runs(client)
    assert all(r["checked"] == 1 for r in rows if r["outcome"] == "done")


# -- discord.voice -----------------------------------------------------------------------------------------------------


def test_join_a_voice_channel_by_the_switcher_checked_by_the_microphone_onset(client, guard) -> None:
    desk = _desk(client, guard)
    guard["mic"] = [False, False, True]          # engine's baseline, the driver's before, then Discord holds it
    logs: list = []
    got = _run(client, _ctx(client, hands=desk, words=["join lantern-club"], log=logs), {"channel": "lantern-club"},
               key="discord.voice")
    assert got.outcome == "done" and got.done_line == "Joined lantern-club on Discord.", got
    assert desk.keys() == ["ctrl+k", "enter"] and desk.typed() == ["!lantern-club"] and guard["pressed"] == []
    rows = client.store._query("SELECT name_ciphertext, n_obs FROM tastes WHERE kind = 'discord_voice'")
    assert len(rows) == 1 and client.store._dec(rows[0][0]) == "lantern-club" and rows[0][1] == 1.0
    assert "lantern-club" not in str(rows[0][0])                        # kept encrypted
    _no_secrets(logs, client)


def test_join_voice_is_pressed_and_judged_only_when_the_switcher_only_opened_it(client, guard) -> None:
    desk = _desk(client, guard)
    guard["mic"] = [False] * 12 + [True]
    got = _run(client, _ctx(client, hands=desk, words=["join lantern-club"]), {"channel": "lantern-club"},
               key="discord.voice")
    assert got.outcome == "done", got
    assert guard["pressed"] == [(DISCORD_HWND, "Button", "Join Voice")]
    assert ("click", {"hwnd": DISCORD_HWND, "element": 'Button "Join Voice"'}) in desk.judged


def test_already_in_a_call_asks_first_and_a_join_that_never_took_is_said_plainly(client, guard) -> None:
    desk = _desk(client, guard)
    desk.in_call = True
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"],
                            _ctx(client, hands=desk, words=["join lantern-club"]))
    assert got.outcome == "asked" and got.ask == "You're already in a call. Join lantern-club on Discord as well?"
    assert desk.calls == [] and guard["focused"] == []
    guard["mic"] = [False]
    guard["press_outcome"] = "missing"
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club", "join_in_call": True},
                            ["yes join lantern-club"], _ctx(client, hands=desk, words=["yes join lantern-club"]))
    assert got.outcome == "failed" and not got.done and got.handoff is None
    # "no": their call is left as it is, nothing done
    desk.calls.clear()
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club", "join_in_call": False},
                            ["no join lantern-club"], _ctx(client, hands=desk, words=["no join lantern-club"]))
    assert got.outcome == "asked" and got.ask == "Okay, I'll leave your call as it is." and desk.calls == []


def test_a_game_in_front_holds_the_join(client, guard) -> None:
    guard["game"] = "skyrunner"
    desk = _desk(client, guard)
    got = _run(client, _ctx(client, hands=desk, words=["join lantern-club"], game=lambda: "skyrunner"),
               {"channel": "lantern-club"}, key="discord.voice")
    assert got.outcome == "held_game" and desk.calls == [] and guard["focused"] == []


# -- helpers -----------------------------------------------------------------------------------------------------------


def _no_secrets(logs: list, client: Any) -> None:
    text = json.dumps(logs, default=str)
    for secret in SECRETS:
        assert secret not in text, secret
    rows = json.dumps([dict(r) for r in client.store._query("SELECT * FROM skill_runs")], default=str)
    rows += json.dumps([dict(r) for r in client.store._query("SELECT * FROM skill_route_tries")], default=str)
    for secret in SECRETS:
        assert secret not in rows, secret


def test_a_read_taken_before_the_switch_is_not_the_last_word(client, guard, monkeypatch) -> None:
    """The watcher's first read after the keys may still show the old chat; the watcher is asked to read again and the
    chat is open once it does: "another chat" is a no only if it still shows at the end of the wait."""
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["dune_dc"], DISCORD_HWND, "Discord.exe")

    def refresh(hwnd: int) -> bool:
        _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")()
        return True

    monkeypatch.setattr(CH, "_board_refresh", refresh)
    got = _run(client, _ctx(client, hands=desk, words=["message Juno see you at nine"]),
               {"person": "Juno", "text": "see you at nine"})
    assert got.outcome == "done" and desk.typed() == ["@juno.pike", "see you at nine"]


def test_a_title_alone_is_never_the_chats_identity(client, guard) -> None:
    store = client.store
    entry = board_entry(store, client.w["juno_dc"], hwnd=DISCORD_HWND, read_at=NOW + 10 ** 6, process="Discord.exe")
    entry.update(scope="", url=None, messages=[])
    got = CH.chat_state(store, [client.w["juno_dc"]], CH.app_data("discord"), NOW + 1, {"windows": [entry]})
    assert got == (None, {"board": "no_identity"})


def test_a_person_with_no_chat_in_these_apps_goes_to_the_thinker(client, guard) -> None:
    from _sweb_world import contact, route, thread

    tid = thread(client.store, "Pigeon", "Pigeon", None, "pigeon.exe", "Rook Vale", mine=3)
    route(client.store, contact(client.store, "Rook Vale"), "pigeon", "dm", "Rook Vale", None, [tid])
    got = _run(client, _ctx(client, hands=_desk(client, guard), words=["message Rook"]), {"person": "Rook"})
    assert got.outcome == "handed_over" and guard["focused"] == [] and guard["opened"] == []


# -- review round 1 ----------------------------------------------------------------------------------------------------


def _capture(client: Any, tid: int, text: str, n: int = 777) -> None:
    """The watcher captures the user's own message ``text`` in thread ``tid`` (after any run's start)."""
    msg(client.store, tid, n, me=True, at=NOW + 5, text=text)
    client.store._write(lambda conn: conn.execute("UPDATE messages SET first_seen = ? WHERE id = (SELECT max(id) "
                                                  "FROM messages)", (10 ** 10,)))


def test_r1_m1_a_send_key_that_went_out_with_no_visible_change_is_pressed_not_unsent(client, guard) -> None:
    """Review r1 M1: Enter was dispatched but its screen watch saw no change (the run reports no_effect). Yuki must
    never say "I didn't send it": it reads memory's capture and says "couldn't confirm it landed" when it isn't
    seen, or "Sent" when it is."""
    for captured in (False, True):
        desk = _desk(client, guard)
        presses = {"n": 0}
        real = desk.dispatch

        def dispatch(name: str, args: dict[str, Any], _real: Any = real, _presses: dict = presses,
                     _captured: bool = captured) -> Any:
            out = _real(name, args)
            if name == "hotkey" and args.get("keys") == ["enter"]:
                _presses["n"] += 1
                if _presses["n"] == 1:
                    _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")()
                else:
                    if _captured:
                        _capture(client, client.w["juno_dc"], "see you at nine", n=800 + _presses["n"])
                    return SimpleNamespace(ok=True, payload={"details": {"verified": False}}, summary="no change",
                                           elapsed_ms=1.0)
            return out

        desk.dispatch = dispatch
        got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
                   {"person": "Juno", "text": "see you at nine", "send": "send"})
        assert desk.keys() == ["ctrl+k", "enter", "enter"]                    # the send key went out
        assert "didn't send" not in (got.say or "")
        if captured:
            assert got.outcome == "done" and got.done_line == "Sent to Juno Pike on Discord.", got
        else:
            assert got.outcome == "unverified"
            assert got.say == "I pressed send to Juno Pike, but couldn't confirm it landed."


def test_r1_m5_a_send_in_an_old_style_whatsapp_chat_is_confirmed_by_its_thread(client, guard) -> None:
    """Review r1 M5: threads kept before 2 Oct carry app "WhatsApp.Root" (process whatsapp.root); the send is matched
    by the route's own capture thread, never by the app's name, so it is confirmed."""
    from _sweb_world import contact, route, thread

    tid = thread(client.store, "WhatsApp.Root", "WhatsApp", None, "WhatsApp.Root.exe", "Wren Ashby", mine=5)
    assert client.store._query("SELECT app FROM threads WHERE id = ?", (tid,))[0][0] == "WhatsApp.Root"
    route(client.store, contact(client.store, "Wren Ashby"), "whatsapp.root", "dm", "Wren Ashby", "+44 7700 900456",
          [tid])
    desk = _desk(client, guard, WA_HWND, "WhatsApp.Root.exe")
    desk.front = WA_HWND
    guard["on_open"] = lambda uri: _open_on(guard, client, tid, WA_HWND, "WhatsApp.Root.exe")()
    desk.effects[("hotkey", "enter")] = lambda: _capture(client, tid, "otter cove")
    got = _run(client, _ctx(client, hands=desk, words=["send otter cove to Wren"]),
               {"person": "Wren", "text": "otter cove", "send": "send"})
    assert guard["opened"] == ["whatsapp://send?phone=447700900456"]
    assert got.outcome == "done" and got.done_line == "Sent to Wren Ashby on WhatsApp.", got


def test_r1_l7_the_same_text_in_another_chat_of_that_app_is_not_this_send(client, guard) -> None:
    """Review r1 L7: a matching message captured in another chat of the same app never counts as "Sent to X"."""
    since = NOW + 100
    juno, dune = client.w["juno_dc"], client.w["dune_dc"]
    result = SimpleNamespace(facts_ram={"app": "discord", "thread_ids": [juno], "chat": True, "typed": True,
                                        "pressed": True})
    ctx = D.ProbeContext(since=since, memory_call=lambda what, fn: fn(client), result=result)
    args = {"app": "discord", "person": "Juno", "text": "otter cove", "send": "send"}
    _capture(client, dune, "otter cove", n=901)                            # in Mira Dune's chat, not Juno's
    got = CH.probe_message_done(ctx, args)
    assert got[0] is None and got[1]["other_chats"] >= 1
    _capture(client, juno, "otter cove", n=902)
    assert CH.probe_message_done(ctx, args)[0] is True


def test_r1_l1_the_name_is_never_typed_when_the_switcher_did_not_open(client, guard) -> None:
    """Review r1 L1: if no other field took the keyboard after Ctrl+K, nothing is typed and Enter is never pressed
    (the name would go into the open chat and be sent)."""
    desk = _desk(client, guard)
    desk.switcher_opens = False
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert desk.keys() == ["ctrl+k"] and desk.typed() == []
    assert got.outcome == "handed_over" and not got.done                  # nothing typed: the thinker, pinned
    desk = _desk(client, guard)
    desk.switcher_opens = False
    got = _run(client, _ctx(client, hands=desk, words=["join lantern-club"]), {"channel": "lantern-club"},
               key="discord.voice")
    assert desk.keys() == ["ctrl+k"] and desk.typed() == [] and guard["pressed"] == [] and not got.done


def test_r1_l6_a_held_send_key_after_typing_is_told_and_a_rerun_never_types_twice(client, guard) -> None:
    """Review r1 L6: Yuki's own check holds the send key after the text was typed: the user is told it's typed and not
    sent (a statement, not a yes/no question that would re-run the skill)."""
    desk = _desk(client, guard)
    desk.effects[("hotkey", "enter")] = _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")
    n = {"enter": 0}
    real_auth = desk.authorize

    def authorize(tool: str, tool_input: dict[str, Any], target: Any = None, earlier: Any = None, *,
                  force: bool = False) -> Any:
        if tool == "hotkey" and tool_input.get("keys") == ["enter"]:
            n["enter"] += 1
            if n["enter"] == 2:                                           # the send key
                return SimpleNamespace(allowed=False, question="Send it to Juno?", why="ask")
        return real_auth(tool, tool_input, target, earlier, force=force)

    desk.authorize = authorize
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "asked" and not got.done
    assert got.ask == ("It's typed in your chat with Juno Pike, but I held back sending it. Press Enter when you want "
                       "it to go.")
    assert "?" not in got.ask
    assert desk.typed() == ["@juno.pike", "see you at nine"] and desk.keys() == ["ctrl+k", "enter"]
    # if the skill runs again anyway ("send it"), the draft Discord kept is never typed a second time
    assert desk.composer == "see you at nine"
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno", "yes send it"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert desk.typed() == ["@juno.pike", "see you at nine", "@juno.pike"]
    assert desk.keys() == ["ctrl+k", "enter", "ctrl+k", "enter", "enter"] and desk.composer == ""
    assert got.outcome == "unverified" and got.say == "I pressed send to Juno Pike, but couldn't confirm it landed."


def test_r1_l3_an_app_the_user_never_said_is_dropped_and_the_usual_route_stands(client, guard) -> None:
    guard["on_open"] = lambda uri: _open_on(guard, client, client.w["vesper_dc"], DISCORD_HWND, "Discord.exe")()
    got = _run(client, _ctx(client, words=["message Vesper"]), {"person": "Vesper", "app": "whatsapp"})
    assert guard["opened"] == ["discord://-/channels/@me/111122223333"]
    assert got.outcome == "done" and got.done_line == "Your chat with Vesper Lark is open on Discord."


def test_r1_m4_a_game_and_a_live_call_never_send_input_to_the_game(client, guard) -> None:
    """Review r1 M4 (the part sWeb owns): with a game in front during a call, no input reaches the game on the ask or
    after the user's "yes" (the resolver's order is sCore's: see the xfail test below)."""
    guard["game"] = "skyrunner"
    desk = _desk(client, guard)
    desk.in_call = True
    for slots, words in (({"channel": "lantern-club"}, ["join lantern-club"]),
                         ({"channel": "lantern-club", "join_in_call": True}, ["yes join lantern-club"])):
        got = E.resolve_and_run("discord.voice", slots, words,
                                _ctx(client, hands=desk, words=words, game=lambda: "skyrunner"),
                                live=Live(game="skyrunner", in_call=True))
        assert got.outcome in ("asked", "held_game") and not got.done
    got = E.resolve_and_run("message", {}, ["message"], _ctx(client, hands=desk, words=["message"],
                                                              game=lambda: "skyrunner"), live=Live(game="skyrunner"))
    assert not got.done
    assert desk.calls == [] and guard["opened"] == [] and guard["focused"] == []


def test_r2_l1_a_game_in_front_during_a_call_answers_after_the_round(client, guard) -> None:
    """Review r2 L1 (the discord.voice part, fixed in sWeb's own files): the in-call question is asked in the rung,
    after the engine's game check, so a game in front during a call is "after the round?", never the question."""
    from yuki.skills.resolve import Held

    got = resolve("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"], store=client.store,
                  live=Live(game="skyrunner", in_call=True))
    assert isinstance(got, Held) and got.say == "You're in a game: after the round?"
    guard["game"] = "skyrunner"
    desk = _desk(client, guard)
    desk.in_call = True
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"],
                            _ctx(client, hands=desk, words=["join lantern-club"], game=lambda: "skyrunner"),
                            live=Live(game="skyrunner", in_call=True))
    assert got.outcome == "held_game" and got.say == "You're in a game: after the round?"
    assert desk.calls == [] and guard["focused"] == []


def test_r1_m4_a_game_in_front_with_no_person_answers_after_the_round(client) -> None:
    # sWire: sCore's resolver now holds an all-focus skill before any question (the xfail marker is gone)
    from yuki.skills.resolve import Held

    got = resolve("message", {}, ["message"], store=client.store, live=Live(game="skyrunner"))
    assert isinstance(got, Held)


# -- review round 2 ----------------------------------------------------------------------------------------------------


def _kept_draft_world(client: Any, guard: dict[str, Any], desk: Any, draft: str, sent: list[str]) -> None:
    """Juno's chat opens on the switcher's Enter holding ``draft`` (the app's kept draft); every Enter pressed in the
    composer records what it would send, and the watcher captures it."""
    presses = {"n": 0}

    def enter() -> None:
        presses["n"] += 1
        if presses["n"] == 1:
            _open_on(guard, client, client.w["juno_dc"], DISCORD_HWND, "Discord.exe")()
            desk.composer = draft

    real = desk.dispatch

    def dispatch(name: str, args: dict[str, Any]) -> Any:
        if name == "hotkey" and args.get("keys") == ["enter"] and desk.field[1] == "Message":
            sent.append(desk.composer)
            _capture(client, client.w["juno_dc"], desk.composer, n=950 + len(sent))
        return real(name, args)

    desk.dispatch = dispatch
    desk.effects[("hotkey", "enter")] = enter


def test_r2_h1_a_chat_holding_other_unsent_text_gets_nothing_typed_and_nothing_sent(client, guard) -> None:
    """Review r2 H1: the chat opens with the user's own half-typed draft in the composer. "Send see you at nine to
    Juno" types nothing and presses nothing (Enter would send the draft with it), and says so plainly; a draft run
    doesn't append either."""
    for mode, words in (("send", "send see you at nine to Juno"), ("draft", "message Juno see you at nine")):
        desk = _desk(client, guard)
        sent: list[str] = []
        _kept_draft_world(client, guard, desk, "hey are you free ", sent)
        logs: list = []
        got = _run(client, _ctx(client, hands=desk, words=[words], log=logs),
                   {"person": "Juno", "text": "see you at nine", "send": mode})
        assert got.outcome == "asked" and not got.done, (mode, got)
        assert got.say == "There's already unsent text in your chat with Juno Pike, so I left it."
        assert desk.typed() == ["@juno.pike"] and desk.keys() == ["ctrl+k", "enter"], mode   # 0 typed, 0 send
        assert sent == [] and desk.composer == "hey are you free ", mode                    # the draft untouched
        assert "hey are you free" not in json.dumps(logs, default=str)
        _no_secrets(logs, client)


def test_r2_h1_a_send_is_done_only_when_the_captured_message_equals_the_text(client, guard) -> None:
    """Review r2 H1: a captured message that only contains the user's words (something went with them) is never
    "Sent": "couldn't confirm". Equal once whitespace and case are folded: "Sent"."""
    since = NOW + 100
    juno = client.w["juno_dc"]
    result = SimpleNamespace(facts_ram={"app": "discord", "thread_ids": [juno], "chat": True, "typed": True,
                                        "pressed": True})
    ctx = D.ProbeContext(since=since, memory_call=lambda what, fn: fn(client), result=result)
    args = {"app": "discord", "person": "Juno", "text": "see you at nine", "send": "send"}
    _capture(client, juno, "hey are you free see you at nine", n=960)
    assert CH.probe_message_done(ctx, args)[0] is None
    assert CH.sent_in_chat(ctx, "see you at nine", [juno])[0] is None
    _capture(client, juno, "  See you   at NINE ", n=961)
    assert CH.probe_message_done(ctx, args)[0] is True


def test_r2_h1_an_exact_draft_is_never_retyped_and_an_empty_composer_is_typed(client, guard) -> None:
    """The composer read decides only what it can see: exactly the user's words there -> only the send key (r1 L6);
    an empty composer -> typed and sent."""
    desk = _desk(client, guard)
    sent: list[str] = []
    _kept_draft_world(client, guard, desk, "see you at nine", sent)
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert desk.typed() == ["@juno.pike"] and sent == ["see you at nine"]
    assert got.outcome == "done" and got.done_line == "Sent to Juno Pike on Discord."
    desk = _desk(client, guard)
    sent = []
    _kept_draft_world(client, guard, desk, "", sent)
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"]),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert desk.typed() == ["@juno.pike", "see you at nine"] and sent == ["see you at nine"]
    assert got.outcome == "done"


def _unreadable_after_open(desk: Any, read: Any, *, times: int | None = None) -> None:
    """Once the chat opened (the switcher's Enter), the composer's focus read answers ``read(desk)`` instead (the
    first ``times`` reads only, when given: then the field answers again)."""
    opened = {"yes": False, "left": times}
    open_chat = desk.effects[("hotkey", "enter")]

    def enter() -> None:
        open_chat()
        opened["yes"] = True

    def focus(timeout_s: float = 0.4) -> Any:
        if opened["yes"] and desk.field[1] == "Message" and (opened["left"] is None or opened["left"] > 0):
            if opened["left"] is not None:
                opened["left"] -= 1
            return read(desk)
        return real(timeout_s)

    desk.effects[("hotkey", "enter")] = enter
    real = desk._focus
    desk._focus = focus


def _no_pattern(desk: Any) -> Any:
    """A composer that takes text but answers neither its Text nor its Value pattern (the real read's shape)."""
    return SimpleNamespace(accepts_text=True, is_password=False, window=desk.front, role="Edit", name="Message",
                           text_source="", value=None, before="", after="")


LINE_UNREAD = ("It's typed in your chat with Juno Pike, but I couldn't read the message box, so I didn't send it. "
               "Press Enter when you want it to go.")


@pytest.mark.parametrize("case,read,times,typed,say", [
    ("no_pattern", _no_pattern, None, True, LINE_UNREAD),
    ("read_timed_out_once", lambda d: None, 1, True, LINE_UNREAD),
    ("read_timed_out", lambda d: None, None, False, "I opened your chat with Juno Pike, but couldn't type it."),
])
def test_sWire_r1_m1_a_send_into_an_unreadable_composer_never_presses_send(client, guard, case, read, times, typed,
                                                                            say) -> None:
    """sWire review r1 M1: Juno's chat opens holding the kept draft "hey are you free " and its composer can't be read
    (it exposes no Text or Value pattern; or the focus read times out, once or throughout). "Send see you at nine to
    Juno" presses no send key (Enter would send the draft with the words): the words are typed when a text field takes
    them, and the user is told; never "done"."""
    desk = _desk(client, guard)
    sent: list[str] = []
    _kept_draft_world(client, guard, desk, "hey are you free ", sent)
    _unreadable_after_open(desk, read, times=times)
    logs: list = []
    got = _run(client, _ctx(client, hands=desk, words=["send see you at nine to Juno"], log=logs),
               {"person": "Juno", "text": "see you at nine", "send": "send"})
    assert got.outcome == "asked" and not got.done and not got.verified, (case, got)
    assert got.say == say, case
    assert desk.keys() == ["ctrl+k", "enter"], case                                     # 0 send keys
    assert desk.typed() == (["@juno.pike", "see you at nine"] if typed else ["@juno.pike"]), case
    assert sent == [], case                                                             # nothing went
    assert desk.composer == ("hey are you free see you at nine" if typed else "hey are you free "), case
    assert _runs(client)[-1]["outcome"] == "asked", case
    assert "hey are you free" not in json.dumps(logs, default=str)
    _no_secrets(logs, client)


def test_sWire_r1_m1_a_composer_reads_empty_only_from_a_source_that_answered() -> None:
    """``_composer_text``: no pattern answered -> unread (``None``), never ""; a Value or Text pattern that read
    nothing -> "" (empty)."""
    def hands(**snap: Any) -> Any:
        base = {"accepts_text": True, "is_password": False, "before": "", "after": "", "value": None,
                "text_source": ""}
        return SimpleNamespace(_focus=lambda timeout_s=0.4: SimpleNamespace(**{**base, **snap}))

    assert CH._composer_text(hands()) is None
    assert CH._composer_state(hands(), "see you at nine") == "unread"
    assert CH._composer_text(hands(text_source="value", value="")) == ""
    assert CH._composer_text(hands(text_source="text")) == ""
    assert CH._composer_text(hands(text_source="text", before="hey ", after="there")) == "hey there"
    assert CH._composer_text(hands(text_source="value", value="hey")) == "hey"
    assert CH._composer_text(SimpleNamespace(_focus=lambda timeout_s=0.4: None)) is None


def test_r2_l3_a_composer_focused_late_never_passes_for_the_switcher(client, guard) -> None:
    """Review r2 L3: no text field has the keyboard right after Discord comes forward; its composer takes it a moment
    later; Ctrl+K opens nothing. The settled read makes the composer the "before", so nothing is typed or sent."""
    desk = _desk(client, guard)
    desk.switcher_opens = False
    real_focus = desk._focus
    calls = {"n": 0}

    def late_focus(timeout_s: float = 0.4) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else real_focus(timeout_s)

    desk._focus = late_focus
    got = _run(client, _ctx(client, hands=desk, words=["message Juno"]), {"person": "Juno"})
    assert desk.typed() == [] and desk.keys() == ["ctrl+k"] and not got.done


def test_r2_m2_the_in_call_question_says_what_will_happen(client, guard, monkeypatch) -> None:
    """Review r2 M2: no route leaves another app's call, so none is promised: a call in another app (or one memory
    can't name) -> "Join {channel} on Discord as well?"; Discord's own call (it holds the microphone, or memory's open
    calls name it) -> "Switch to {channel}?". The words are the connection file's data."""
    data = CH.app_data("discord")
    assert data["asks"]["in_call"] == "You're already in a call. Join {channel} on Discord as well?"
    assert data["asks"]["switch"] == "Switch to {channel}?"
    for calls, mic, want in (([], False, "You're already in a call. Join lantern-club on Discord as well?"),
                             (["zen"], False, "You're already in a call. Join lantern-club on Discord as well?"),
                             (["discord"], False, "Switch to lantern-club?"),
                             ([], True, "Switch to lantern-club?")):
        desk = _desk(client, guard)
        desk.in_call = True
        guard["mic"] = [mic]
        monkeypatch.setattr(CH, "_current_calls", lambda memory, _c=calls: list(_c))
        got = E.resolve_and_run("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"],
                                _ctx(client, hands=desk, words=["join lantern-club"]))
        assert got.outcome == "asked" and got.ask == want, (calls, mic, got.ask)
        assert "Leave" not in got.ask and desk.calls == [] and guard["focused"] == []
    # no call live, Discord not holding the microphone: no question, it joins
    desk = _desk(client, guard)
    desk.in_call = False
    monkeypatch.setattr(CH, "_current_calls", lambda memory: [])
    guard["mic"] = [False, False, True]
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"],
                            _ctx(client, hands=desk, words=["join lantern-club"]))
    assert got.outcome == "done" and desk.keys() == ["ctrl+k", "enter"]


def test_r2_l2_a_plain_yes_alone_fails_safe(client, guard) -> None:
    """Review r2 L2 (carried to sVoice/sCore): the answer turn's words are only ["yes"], so the channel from the
    asking turn isn't in them and is dropped: Yuki asks for the channel again, never guesses, nothing is pressed."""
    desk = _desk(client, guard)
    desk.in_call = True
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club", "join_in_call": True}, ["yes"],
                            _ctx(client, hands=desk, words=["yes"]))
    assert got.outcome == "asked" and got.ask == "Which voice channel?" and desk.calls == []


def test_r2_l5_an_unreadable_microphone_still_waits_before_join_voice(client, guard, monkeypatch) -> None:
    """Review r2 L5: with the microphone state unreadable no onset can show; "Join Voice" is still pressed only after
    the switcher's channel had the same 2 s to open."""
    desk = _desk(client, guard)
    guard["mic"] = [None]
    ctx = _ctx(client, hands=desk, words=["join lantern-club"])
    at: dict[str, float] = {}
    desk.effects[("hotkey", "enter")] = lambda: at.setdefault("enter", ctx.clock())
    monkeypatch.setattr(CH, "_press", lambda hwnd, role, name: at.setdefault("press", ctx.clock()) and "pressed")
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club"}, ["join lantern-club"], ctx, live=Live())
    assert at["press"] - at["enter"] >= CH.VOICE_WAIT_S and not got.done
    assert got.say == "I opened lantern-club in Discord, but couldn't confirm you joined it."
