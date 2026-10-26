"""sWeb: the `message` and `discord.voice` skills (PLAN.md S-2; D0, D17, D19, rule 8; B.2; map-skills S2.5).

Invented people and chats only (``_sweb_world``); a temp database; a stand-in desk for the voice hands, so the real step
runner runs the quick switcher and the typing against it. The drivers' live plumbing (focus, opening a link, the Now
board, the microphone, a button press) is replaced per test, and the real one is made unreachable: nothing here
touches the desktop, a window, the microphone or the real memory.

r30: the end-to-end message and voice-join runs that asserted the 10 Oct key, typing and done-line sequences were cut
(the message path was rebuilt after them: lSplit, zFinal, xL2Grant, lMsg, fMsg, chat19 and loops 2-3); the
driver-level tests (whom and where, deep links, the switcher query, game holds) stay.
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


def test_typing_needs_the_voice_hands_otherwise_the_thinker_gets_it_pinned(client, guard) -> None:
    got = _run(client, _ctx(client, hands=None, words=["message Vesper otter cove"], origin="thinker"),
               {"person": "Vesper", "text": "otter cove"})
    assert got.outcome == "handed_over" and got.handoff and got.handoff["key"] == "message"
    assert guard["opened"] == [] and guard["focused"] == []
    assert got.handoff["done"]["probe"] == "driver" and got.handoff["done"]["call"] == "message_done"


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


def test_r2_l2_a_plain_yes_alone_fails_safe(client, guard) -> None:
    """Review r2 L2 (carried to sVoice/sCore): the answer turn's words are only ["yes"], so the channel from the
    asking turn isn't in them and is dropped: Yuki asks for the channel again, never guesses, nothing is pressed."""
    desk = _desk(client, guard)
    desk.in_call = True
    got = E.resolve_and_run("discord.voice", {"channel": "lantern-club", "join_in_call": True}, ["yes"],
                            _ctx(client, hands=desk, words=["yes"]))
    assert got.outcome == "asked" and got.ask == "Which voice channel?" and desk.calls == []

