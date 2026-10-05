"""honest (report 87, plan row 4 of report 76): one outcome verdict, repeated and never upgraded.

The verdict (``yuki.agent.finish.outcome_verdict``) from run-trace facts; the agent carrying it on its Final; the
runtime's outcome signal; the voice saying a result at its level (never by code below likely, the talker's note with
worked examples, the owed ledger's fallback line); the talker's task view; the done card, the typed reply, the
island's done flash and the window's run line. The live false claims of 28 Sep - 5 Oct are the cases, rebuilt with
invented names.

Fakes and stand-ins only: no network, no desktop, no microphone, no real memory, nothing sent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication

from yuki.agent.finish import (EXPECTED_UNVERIFIABLE, LINE_FAILED, LINE_PARTIAL, LINE_SEND_UNCONFIRMED, LINE_UNREAD,
                               LINE_UNVERIFIED, outcome_verdict, sayable)

APP = QApplication.instance() or QApplication([])   # widgets below: a full application first


def _act(tool: str, *, ok: bool = True, verdict: bool | None = True, why: str = "", app: str | None = "Discord",
         what: str = "", **extra: Any) -> dict[str, Any]:
    return {"kind": "action", "tool": tool, "ok": ok, "verdict": verdict, "why": why, "app": app,
            "input": {"what": what} if what else {}, **extra}


def _read(tool: str = "look_at_window") -> dict[str, Any]:
    return {"kind": "read", "tool": tool, "ok": True, "verdict": None, "why": "", "app": "Discord", "input": {}}


# -- the verdict itself --------------------------------------------------------------------------------------------


def test_an_answer_with_nothing_done_is_an_answer_unless_it_tells_the_screen_unread() -> None:
    plain = outcome_verdict([_read()])
    assert plain["level"] == "answer" and not plain["acted"] and plain["line"] == "" and sayable(plain)
    unread = outcome_verdict([], unbacked=["it says how window 9 is now, but this request has not read it"])
    assert unread["level"] == "unverified" and unread["line"] == LINE_UNREAD and not sayable(unread)
    assert unread["label"] == "Not confirmed"


def test_the_live_whatsapp_send_the_check_never_saw_is_not_confirmed() -> None:
    # 5 Oct: the send check said not confirmed and the voice said "sent"
    steps = [_act("type_text", why="the field reads back the text", app="WhatsApp"),
             _act("press", verdict=None, why="the message did not show up in memory's capture of the chat within "
                  "4 s: not confirmed", app="WhatsApp", send="unconfirmed", send_sig="5a1e",
                  what="Sending the message")]
    v = outcome_verdict(steps)
    assert (v["level"], v["send"], v["line"]) == ("unverified", "unconfirmed", LINE_SEND_UNCONFIRMED)
    assert v["seen"] == "the message did not show up in the chat" and v["step"] == "Sending the message"
    assert not sayable(v)
    # a later verified click in the same app never puts an unseen send right; only a confirmed send does
    assert outcome_verdict(steps + [_act("click", app="WhatsApp")])["level"] == "unverified"
    # second review V2-2: WhatsApp's title never names the chat, so even the same text seen later may be another chat's
    again = outcome_verdict(steps + [_act("press", app="WhatsApp", send="confirmed", send_sig="5a1e")])
    assert (again["level"], again["send"]) == ("unverified", "unconfirmed")
    # in an app whose title names the chat, the same message seen in the same chat puts it right
    chat = [{**s, "app": "Discord", "window": "Ana - Discord"} for s in steps]
    seen = outcome_verdict(chat + [_act("press", app="Discord", send="confirmed", send_sig="5a1e",
                                        window="Ana - Discord")])
    assert (seen["level"], seen["send"], seen["seen"]) == ("verified", "confirmed", "the message shows in the chat")


def test_the_live_discord_send_after_failed_typing_did_not_work() -> None:
    # the typing failed, then Enter went in with no text known to look for: the loop tags it ``unchecked`` (review F3)
    v = outcome_verdict([_act("type_text", ok=False, verdict=False, why="it failed", what="Typing the message"),
                         _act("press", why="something visibly changed after it", send="unchecked")])
    assert v["level"] == "failed" and v["line"] == LINE_FAILED and not v["partial"] and v["step"] == "Typing the message"
    assert v["send"] == "unconfirmed"
    # the Enter alone: a send nothing could check is not confirmed, whatever its own report saw
    alone = outcome_verdict([_act("press", why="something visibly changed after it", send="unchecked")])
    assert (alone["level"], alone["send"], alone["line"]) == ("unverified", "unconfirmed", LINE_SEND_UNCONFIRMED)
    assert alone["seen"] == "there was no message text known to look for in the chat"
    # review F3 (C): a later verified focus in the same app never puts it right
    assert outcome_verdict([_act("press", why="x", send="unchecked", window="Ana - Discord"),
                            _act("focus_window")])["level"] == "unverified"


def test_a_window_close_verified_on_the_window_says_so() -> None:
    closed = _act("window_action", why="its report reads back the new state", app="Slack")
    closed["input"] = {"action": "close"}
    v = outcome_verdict([closed])
    assert v["level"] == "verified" and v["acted"] and sayable(v) and v["seen"] == "its window is gone"
    from yuki.agent.finish import finish_line

    # live: "Slack's closed" while it still ran in the tray; the close is measured on its window
    assert finish_line("window_action", {"action": "close"}, process="slack.exe") == "Slack's window is closed."


def test_a_key_with_nothing_watched_is_not_confirmed_and_a_retry_that_verified_puts_it_right() -> None:
    # live: "Warp's full screen" on a key whose report watched nothing
    key = _act("hotkey", verdict=None, why="nothing its report watched could show the effect", app="Warp")
    v = outcome_verdict([key])
    assert v["level"] == "unverified" and v["line"] == LINE_UNVERIFIED
    assert v["seen"] == "nothing on screen showed whether it worked"
    # the verified window change in Warp the brain marked as completing the request puts it right (same app)
    fixed = outcome_verdict([key, _act("window_action", why="its report reads back the new state", app="Warp")],
                            finished_by_action=True)
    assert fixed["level"] == "verified" and fixed["open"] == 0
    # review F1: a verified focus in Warp that is not the finishing action does not: the key's effect was never seen
    assert outcome_verdict([key, _act("focus_window", app="Warp")])["level"] == "unverified"
    # backed by a later verified step in the same window (the loop's ``confirmed``): likely, never verified
    backed = outcome_verdict([{**key, "confirmed": True}, _act("focus_window", app="Warp")])
    assert backed["level"] == "likely"
    # a later verified step in another app does not put it right
    assert outcome_verdict([key, _act("launch_app", app="Spotify")])["level"] == "unverified"
    # a retry (the same operation in the same app) stands for it, whatever its own level
    first = {**_act("window_action", ok=False, verdict=False, why="it failed", app="Slack"), "input": {"action": "close"}}
    retry = outcome_verdict([first, {**_act("window_action", verdict=None, why="x", app="Slack"),
                                     "input": {"action": "close"}}])
    assert retry["level"] == "unverified"


def test_part_done_part_failed_is_partly_done_and_a_verified_finishing_action_ends_it() -> None:
    steps = [_act("launch_app", app="Spotify"),
             _act("click", verdict=False, why="focus, window, its state and page were watched and nothing changed",
                  app="Slack", what="Quitting Slack")]
    v = outcome_verdict(steps)
    assert (v["level"], v["partial"], v["line"], v["step"]) == ("failed", True, LINE_PARTIAL, "Quitting Slack")
    # replayed 5 Oct: a slider that failed, then the mixer level for the same app the brain marked as finishing
    tries = [_act("set_slider", ok=False, verdict=False, why="it failed", app="Spotify.exe"),
             {**_act("app_volume", why="its report says it was seen", app=None), "input": {"app": "spotify.exe"}}]
    assert outcome_verdict(tries, finished_by_action=True)["level"] == "verified"


def test_review_f1_a_finishing_action_in_another_app_never_puts_a_failed_step_right() -> None:
    # "make Warp full screen and open Spotify": the full screen was contradicted, Spotify opened and finished it
    warp = _act("window_action", verdict=False, why="its state was watched and nothing changed", app="warp.exe",
                what="Making Warp full screen", call=1)
    warp["input"] = {"action": "fullscreen", "what": "Making Warp full screen"}
    spotify = _act("launch_app", app="Spotify.exe", call=2)
    v = outcome_verdict([warp, spotify], finished_by_action=True)
    assert (v["level"], v["partial"], v["step"]) == ("failed", True, "Making Warp full screen")
    assert not sayable(v)
    # a step of the finishing response itself whose check is weak by nature is covered by it; second review V2-6: one
    # in another app that nothing checked is not
    same = outcome_verdict([_act("type_text", verdict=None, why=EXPECTED_UNVERIFIABLE, app="Spotify.exe", call=3),
                            _act("launch_app", app="Slack.exe", call=3)], finished_by_action=True)
    assert same["level"] == "verified"
    blind = outcome_verdict([_act("press", verdict=None, why="x", app="Spotify.exe", call=3),
                             _act("launch_app", app="Slack.exe", call=3)], finished_by_action=True)
    assert (blind["level"], blind["open"]) == ("unverified", 1)
    # never a contradicted one, even in the same response
    assert outcome_verdict([_act("press", verdict=False, why="x", app="Notepad.exe", call=3),
                            _act("launch_app", app="Slack.exe", call=3)], finished_by_action=True)["level"] == "failed"


def test_review_f2_a_confirmed_send_puts_right_only_the_same_message_in_the_same_app() -> None:
    # "tell Ana I'm late on WhatsApp and Bob on Discord": the first never seen, the second seen
    ana = _act("press", verdict=None, why="not confirmed", app="WhatsApp.Root.exe", send="unconfirmed",
               send_sig="aaaa", what="Sending Ana the message")
    bob = _act("press", app="Discord.exe", send="confirmed", send_sig="bbbb")
    v = outcome_verdict([ana, bob])
    assert (v["level"], v["send"], v["line"]) == ("unverified", "unconfirmed", LINE_SEND_UNCONFIRMED)
    # the send summary looks at every send, not the last: a confirmed send first, an unseen one after
    assert outcome_verdict([bob, ana])["send"] == "unconfirmed"
    # another message in the same app does not put it right either; the same message sent again does
    other = _act("press", app="WhatsApp.Root.exe", send="confirmed", send_sig="cccc")
    assert outcome_verdict([ana, other])["level"] == "unverified"
    named = {**ana, "app": "Slack.exe", "window": "Ana (DM) - Acme - Slack"}
    again = outcome_verdict([named, {**other, "app": "Slack.exe", "send_sig": "aaaa",
                                     "window": "Ana (DM) - Acme - Slack"}])
    assert (again["level"], again["send"]) == ("verified", "confirmed")
    # no text known for the first: only a confirmed send in the same chat window (by its title) puts it right
    blind = _act("press", why="x", app="Discord.exe", send="unchecked", window="Ana - Discord")
    assert outcome_verdict([blind, {**bob, "window": "Bob - Discord"}])["level"] == "unverified"
    assert outcome_verdict([blind, {**bob, "window": "Ana - Discord"}])["level"] == "verified"


def test_review_f3_an_unrelated_verified_step_never_puts_a_failed_one_right() -> None:
    close = _act("window_action", verdict=False, why="the window is still there", app="slack.exe", what="Quitting Slack")
    close["input"] = {"action": "close", "what": "Quitting Slack"}
    # (B) the close was contradicted, then a focus on Slack verified: still did not work
    b = outcome_verdict([close, _act("focus_window", app="slack.exe")])
    assert b["level"] == "failed" and b["seen"] == "the screen showed it didn't happen"
    # (D) a failed press in Chrome, then a verified step whose app is not known
    d = outcome_verdict([_act("press", ok=False, verdict=False, why="it failed", app="chrome.exe"),
                         _act("open_url", app=None)])
    assert d["level"] == "failed"
    # a retry of the same operation in the same app does stand for it
    retry = {**close, "verdict": True, "why": "its report reads back the new state"}
    assert outcome_verdict([close, retry])["level"] == "verified"


def test_review_f4_a_focus_before_a_failed_close_is_not_a_part_done() -> None:
    from yuki.ui.step_words import return_view
    from yuki.voice.mouth import result_payload

    close = _act("window_action", verdict=False, why="the window is still there", app="slack.exe",
                 what="Quitting Slack")
    close["input"] = {"action": "close", "what": "Quitting Slack"}
    v = outcome_verdict([_act("focus_window", app="slack.exe"), close])
    assert (v["level"], v["partial"], v["line"]) == ("failed", False, LINE_FAILED)
    assert "partly" not in result_payload("done", "Closed Slack.", outcome=v)
    view = return_view("done", "Closed Slack.", [], verdict=v)
    assert view["head"] == "Didn’t work"
    # a verified change the user asked for, or a step in another app, is a part done
    shrink = _act("window_action", app="slack.exe")
    shrink["input"] = {"action": "minimize"}
    assert outcome_verdict([shrink, close])["partial"] is True
    assert outcome_verdict([_act("launch_app", app="Spotify.exe"), close])["partial"] is True


def _window(action: str, **kw: Any) -> dict[str, Any]:
    step = _act("window_action", **kw)
    step["input"] = {"action": action, **({"what": kw["what"]} if kw.get("what") else {})}
    return step


def test_second_review_v2_1_a_retry_aims_at_the_same_effect_not_just_the_same_tool() -> None:
    close = _window("close", verdict=False, why="the window is still there", app="slack.exe", what="Quitting Slack")
    # a contradicted close, then a verified window_action focus of Slack: a focus is never a retry of a close
    v = outcome_verdict([close, _window("focus", app="slack.exe")])
    assert (v["level"], v["open"], v["line"]) == ("failed", 1, LINE_FAILED) and not sayable(v)
    # nor when the focus is the action the brain marked as finishing
    assert outcome_verdict([close, _window("focus", app="slack.exe")], finished_by_action=True)["level"] == "failed"
    # a failed click on one control, then a verified click on another in the same app
    submit = _act("click", ok=False, verdict=False, why="it failed", app="chrome.exe", control="Button|Submit")
    banner = _act("click", app="chrome.exe", control="Button|Close the cookie banner")
    assert outcome_verdict([submit, banner])["level"] == "failed"
    # the same control again does stand for it (its role and name from the trace's target when no control is kept)
    again = _act("click", app="chrome.exe", target='Button "Submit", window "Form - Chrome"')
    assert outcome_verdict([{**submit, "control": None, "target": again["target"]}, again])["level"] == "verified"
    assert outcome_verdict([submit, {**banner, "control": "Button|Submit"}])["level"] == "verified"
    # a likely typing into one field is not made verified by a verified typing into another
    typed = _act("type_text", verdict=None, why=EXPECTED_UNVERIFIABLE, app="discord.exe", control="Edit|Search")
    other = _act("type_text", app="discord.exe", control="Edit|Message @Ana")
    assert outcome_verdict([typed, other])["level"] == "likely"
    # an unknown app is never the same app
    blind = _act("click", ok=False, verdict=False, why="it failed", app=None, control="Button|Submit")
    assert outcome_verdict([blind, {**blind, "ok": True, "verdict": True, "why": ""}])["level"] == "failed"
    # keys: a different key is not a retry; the same key is
    f11 = {**_act("press", ok=False, verdict=False, why="it failed", app="warp.exe"), "input": {"key": "f11"}}
    assert outcome_verdict([f11, {**_act("press", app="warp.exe"), "input": {"key": "escape"}}])["level"] == "failed"
    assert outcome_verdict([f11, {**_act("press", app="warp.exe"), "input": {"key": "F11"}}])["level"] == "verified"
    # a scroll at a point (no element): the same way again is a retry, the other way is not
    down = {**_act("scroll", ok=False, verdict=False, why="it failed", app="arc.exe"), "input": {"dy": -3}}
    assert outcome_verdict([down, {**_act("scroll", app="arc.exe"), "input": {"dy": -5}}])["level"] == "verified"
    assert outcome_verdict([down, {**_act("scroll", app="arc.exe"), "input": {"dy": 3}}])["level"] == "failed"


def test_second_review_v2_2_and_3_the_same_text_to_two_chats_or_a_blind_send_stays_open() -> None:
    ana = _act("press", verdict=None, why="not confirmed", app="WhatsApp.exe", send="unconfirmed",
               send_sig="abc123abc123", window="WhatsApp", what="Sending Ana the message")
    bob = _act("press", app="WhatsApp.exe", send="confirmed", send_sig="abc123abc123", window="WhatsApp")
    v = outcome_verdict([ana, bob])
    assert (v["level"], v["send"], v["open"]) == ("unverified", "unconfirmed", 1)
    # Discord names the channel: the same text in two channels is two messages
    general = {**ana, "app": "Discord.exe", "window": "#general | Acme - Discord"}
    random = {**bob, "app": "Discord.exe", "window": "#random | Acme - Discord"}
    assert outcome_verdict([general, random])["send"] == "unconfirmed"
    # V2-3: Enter with no text known in WhatsApp, then another message confirmed there: still open
    blind = _act("press", why="x", app="WhatsApp.exe", send="unchecked", window="WhatsApp")
    later = {**bob, "send_sig": "ffff00001111"}
    assert outcome_verdict([blind, later])["level"] == "unverified"
    assert outcome_verdict([{**blind, "window": "(3) WhatsApp"}, {**later, "window": "(3) WhatsApp"}])["level"] == \
        "unverified"


def test_second_review_v2_4_the_card_never_checks_a_step_the_verdict_did_not_see() -> None:
    from yuki.ui.step_words import label_input, return_view

    close = _window("close", verdict=False, why="the window is still there", app="slack.exe", what="Quitting Slack")
    calls = [{"name": "window_action", "ok": True, "input": label_input({"action": "close", "what": "Quitting Slack",
                                                                          "_about": {"app": "Slack"}})}]
    view = return_view("done", "Closed Slack.", calls, verdict=outcome_verdict([close]))
    assert view["head"] == "Didn’t work" and not any(r["icon"] == "check" for r in view["rows"])
    full = _window("fullscreen", verdict=None, why="x", app="warp.exe")
    calls = [{"name": "window_action", "ok": True, "input": {"action": "fullscreen", "_about": {"app": "Warp"}}}]
    unv = return_view("done", "Warp's full screen.", calls, verdict=outcome_verdict([full]))
    assert unv["head"] == "Not confirmed" and not any(r["icon"] == "check" for r in unv["rows"])
    # a change whose own check showed it keeps its check beside the one that did not work
    shrink = _window("minimize", app="discord.exe")
    calls = [{"name": "window_action", "ok": True, "input": {"action": "minimize", "_about": {"app": "Discord"}}},
             {"name": "window_action", "ok": True, "input": {"action": "close", "_about": {"app": "Slack"}}}]
    mixed = return_view("done", "Done.", calls, verdict=outcome_verdict([shrink, close]))
    checks = [r["text"] for r in mixed["rows"] if r["icon"] == "check"]
    assert mixed["head"] == "Partly done" and len(checks) == 1 and "Slack" not in checks[0]
    # at verified, as before
    ok = return_view("done", "Done.", calls, verdict=outcome_verdict([shrink, {**close, "verdict": True}]))
    assert ok["head"] == "Done" and [r["icon"] for r in ok["rows"]].count("check") >= 2


def test_second_review_v2_5_the_rest_of_a_held_result_keeps_its_level() -> None:
    from test_voice_v39 import Params, _settle

    async def main() -> Any:
        rt, bridge = _voice_bridge()
        bridge.speak_result = None
        bridge._max_sentences = 1
        params = Params("tell ana and bob")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], "I wrote to them. I sent it to Ana and she has it now. I also told Bob.",
                   UNSEEN_SEND)
        await asyncio.wait_for(task, 1)
        more = Params("")
        await bridge.more_of_result(more)
        return params.results[-1][0], more.results[-1][0]

    first, rest = asyncio.run(main())
    assert first["outcome"] == "not confirmed" and first["more_available"] is True
    assert rest["more"] and rest["outcome"] == "not confirmed"
    assert rest["checked"] == "the message did not show up in the chat"
    assert "couldn't confirm it went" in rest["how_to_say_it"]


def test_second_review_v2_6_a_finishing_action_leaves_an_unchecked_step_in_another_app_open() -> None:
    key = {**_act("press", verdict=None, why="x", app="warp.exe", call=4), "input": {"key": "f11"}}
    v = outcome_verdict([key, _act("launch_app", app="spotify.exe", call=4)], finished_by_action=True)
    assert (v["level"], v["open"]) == ("unverified", 1) and not sayable(v)


def test_second_review_v2_7_a_focus_never_backs_an_unchecked_step() -> None:
    from yuki.agent.loop import Agent

    trace = [{"kind": "action", "tool": "press", "ok": True, "verdict": None, "why": "x", "app": "warp.exe",
              "input": {"key": "f11"}},
             {"kind": "action", "tool": "focus_window", "ok": True, "verdict": True, "why": "", "app": "warp.exe",
              "input": {"hwnd": 100}}]
    agent = SimpleNamespace(_trace=trace[:1], _unconfirmed=[])
    Agent._mark_confirmations(agent, 100, (None, "x"), "press", {"key": "f11"})
    agent._trace.append(trace[1])
    Agent._mark_confirmations(agent, 100, (True, ""), "focus_window", {"hwnd": 100})
    assert trace[0]["confirmed"] and trace[0]["confirmed_by_ready"]   # practice memory still sees it backed
    v = outcome_verdict(agent._trace)
    assert v["level"] == "unverified" and not sayable(v)
    # a later step with an effect of its own in that window still backs it (it stayed open for one)
    agent._trace.append({"kind": "action", "tool": "window_action", "ok": True, "verdict": True, "why": "",
                         "app": "warp.exe", "input": {"action": "maximize"}})
    Agent._mark_confirmations(agent, 100, (True, ""), "window_action", {"action": "maximize"})
    assert "confirmed_by_ready" not in trace[0] and agent._unconfirmed == []
    assert outcome_verdict(agent._trace)["level"] == "likely"


def test_weak_checks_by_nature_are_likely_and_say_why() -> None:
    typed = outcome_verdict([_act("type_text", verdict=None, why=EXPECTED_UNVERIFIABLE)])
    assert typed["level"] == "likely" and sayable(typed) and typed["line"] == ""
    backed = outcome_verdict([_act("click", verdict=None, why="x", confirmed=True)])
    assert backed["level"] == "likely" and backed["seen"] == "a later step showed it worked"
    command = outcome_verdict([_act("run_powershell", verdict=None, why="its report cannot show the effect", app=None)])
    assert command["level"] == "likely" and command["seen"] == "the command ran without an error"


def test_a_claim_nothing_backed_caps_the_level() -> None:
    v = outcome_verdict([_act("launch_app")], unbacked=["it says \"the call started\", but no step showed that"])
    assert v["level"] == "unverified" and v["acted"] and not sayable(v)


# -- the agent carries it ----------------------------------------------------------------------------------------


def test_the_agent_ends_with_the_verdict_on_its_final(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _response, _run, _send_memory
    from yuki.agent.tools import FINISHES_FIELD
    from yuki.log.events import Final

    agent = _agent(Client([_response(_block("tool_use", id="t1", name="window_action",
                                            input={"hwnd": CHROME, "action": "minimize", FINISHES_FIELD: True}))]),
                   tmp_path, judge=False)
    final = _run(agent, "minimize chrome")[-1]
    assert isinstance(final, Final) and final.outcome["level"] == "verified"
    assert final.outcome["seen"] == "its window is minimized" and agent.last_summary["verdict"] == "verified"

    sends = lambda p: {"decision": "allow", "sends": True, "scope": "call", "why": "dictated"}  # noqa: E731
    client = Client([
        _response(_block("tool_use", id="t0", name="check_situation",
                         input={"action": "send the message", "hwnd": CHROME})),
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"text": "on my way", "press_enter": True, "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []})),
    ], judge=sends)
    agent = _agent(client, tmp_path, memory=_send_memory([]))
    final = _run(agent, "tell sam on my way")[-1]
    # fix6: below likely the user gets the verdict's line, never the brain's unbacked "Sent."
    assert isinstance(final, Final) and final.text == LINE_SEND_UNCONFIRMED
    assert (final.outcome["level"], final.outcome["send"]) == ("unverified", "unconfirmed")
    assert final.outcome["line"] == LINE_SEND_UNCONFIRMED
    step = [s for s in agent._trace if s.get("send")][-1]
    assert step["send"] == "unconfirmed" and len(step["send_sig"]) == 12 and "on my way" not in str(step["send_sig"])

    # review F3: Enter pressed as a send with no text known to look for is tagged unchecked, graded not confirmed
    client = Client([
        _response(_block("tool_use", id="t0", name="check_situation",
                         input={"action": "send the message", "hwnd": CHROME})),
        _response(_block("tool_use", id="t1", name="press", input={"key": "enter", "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []})),
    ], judge=sends)
    agent = _agent(client, tmp_path, memory=_send_memory([]))
    final = _run(agent, "send it")[-1]
    blind = [s for s in agent._trace if s.get("send")]
    assert blind and blind[-1]["send"] == "unchecked" and "send_sig" not in blind[-1]
    assert (final.outcome["level"], final.outcome["send"]) == ("unverified", "unconfirmed")


def test_the_runtime_sends_the_verdict_before_the_words() -> None:
    from yuki.log.events import Final
    from yuki.ui import runtime as rt_mod

    src = Path(rt_mod.__file__).read_text(encoding="utf-8")
    at = src.index("elif isinstance(event, Final):")
    block = src[at:at + 500]
    assert block.index("signal.emit") < block.index("runtime.finished.emit")
    assert hasattr(rt_mod.AgentRuntime, "outcome")
    assert Final("x").outcome is None and Final("x") == Final("x", outcome={"level": "verified"})


# -- the voice repeats it ------------------------------------------------------------------------------------------


def _voice_bridge() -> tuple[Any, Any]:
    from test_voice_v39 import FakeRuntime, _bridge

    class Runtime(FakeRuntime):
        outcome = Signal(str, int, dict)

    return _bridge(Runtime())


async def _end(rt: Any, job: int, text: str, verdict: dict | None, *, finishing: str | None = None) -> None:
    from test_voice_v39 import _settle

    rt.started.emit("worker", job, "x")
    rt.tool_called.emit("worker", job, "press", {"key": "enter", **({"finishes_with": finishing} if finishing else {})})
    rt.tool_finished.emit("worker", job, "press", True, "pressed enter")
    if verdict is not None:
        rt.outcome.emit("worker", job, verdict)
    rt.finished.emit("worker", job, text)
    rt.lane_done.emit("worker", job, "x", False, [])
    await _settle(20)


UNSEEN_SEND = outcome_verdict([_act("press", verdict=None, why="not confirmed", send="unconfirmed",
                                    what="Sending the message")])


def test_a_short_sent_with_an_unseen_send_goes_to_the_talker_with_its_level_never_by_code() -> None:
    from test_voice_v39 import Params, _settle
    from yuki.voice.narrator import OwedLedger

    async def main() -> Any:
        rt, bridge = _voice_bridge()
        bridge.ledger = OwedLedger()
        said: list[list[str]] = []

        async def speak_result(lines: list[str]) -> None:
            said.append(lines)

        bridge.speak_result = speak_result
        params = Params("tell sam on my way")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], "Sent.", UNSEEN_SEND, finishing="Sent.")
        await asyncio.wait_for(task, 1)
        return params, said, bridge

    params, said, bridge = asyncio.run(main())
    result, props = params.results[-1]
    assert not said and props.run_llm is True                       # never said by code, the talker says it
    assert result["outcome"] == "not confirmed" and result["checked"] == "the message did not show up in the chat"
    assert "verified" not in result and "couldn't confirm it went" in result["how_to_say_it"]
    assert "never that it worked" in result["how_to_say_it"]
    owed = bridge.ledger.pending[-1]
    assert owed.text == LINE_SEND_UNCONFIRMED                        # the fallback is the verdict's line, not "Sent."
    job = bridge._finished[-1]
    status = job.status(0.0)
    assert status["checked"] == "not confirmed: the message did not show up in the chat"


def test_a_verified_or_likely_result_is_still_said_by_code() -> None:
    from test_voice_v39 import Params, _settle

    verified = outcome_verdict([_act("press", why="something visibly changed after it")])

    async def main(verdict: dict | None) -> list[list[str]]:
        rt, bridge = _voice_bridge()
        said: list[list[str]] = []

        async def speak_result(lines: list[str]) -> None:
            said.append(lines)

        bridge.speak_result = speak_result
        params = Params("pause it")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], "Paused.", verdict, finishing="Paused.")
        await asyncio.wait_for(task, 1)
        return said

    assert asyncio.run(main(verified)) == [["Paused."]]
    assert asyncio.run(main(None)) == [["Paused."]]                  # no verdict (an older agent): as before


def test_the_talkers_notes_per_level_carry_worked_examples_and_a_late_result_keeps_its_level() -> None:
    from yuki.voice.mouth import result_payload

    failed = outcome_verdict([_act("click", verdict=False, why="nothing changed", app="Slack", what="Quitting Slack")])
    body = result_payload("done", "Closed Slack.", outcome=failed)
    assert body["outcome"] == "did not work" and "Quitting Slack" in body["how_to_say_it"]
    assert "E.g." in body["how_to_say_it"]
    late = result_payload("done", "Sent.", late=True, outcome=UNSEEN_SEND)
    assert late["late"] and late["outcome"] == "not confirmed" and "couldn't confirm" in late["how_to_say_it"]
    held = result_payload("done", "Sent.", held_back=True, outcome=UNSEEN_SEND)
    assert held["outcome"] == "not confirmed"                        # asked about later, it answers at its level
    unread = result_payload("done", "The video's paused.",
                            outcome=outcome_verdict([], unbacked=["not read"]))
    assert unread["outcome"] == "not checked" and "haven't checked" in unread["how_to_say_it"]
    plain = result_payload("done", "Paused.", verified=True, outcome=outcome_verdict([_act("press")]))
    assert plain["verified"] is True and "outcome" not in plain
    assert "outcome" not in result_payload("failed", "It broke.", outcome=UNSEEN_SEND)   # a failure says itself


def test_the_talkers_task_line_and_the_next_hand_off_carry_the_level() -> None:
    from test_voice_v39 import Params, _settle

    async def main() -> Any:
        rt, bridge = _voice_bridge()
        bridge.speak_result = None
        params = Params("tell sam on my way")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], "Sent.", UNSEEN_SEND)
        await asyncio.wait_for(task, 1)
        return bridge

    bridge = asyncio.run(main())
    line = bridge.work_line()
    assert "finished, not confirmed (the message did not show up in the chat)" in line and '"Sent."' in line


# -- the done card, the typed reply, the island, the window -----------------------------------------------------


def test_the_done_card_heads_by_the_level_and_never_checks_an_unseen_send() -> None:
    from yuki.ui.step_words import graded_reply, level_row, return_view

    send = [{"name": "type_text", "ok": True,
             "input": {"what": "send Ana the message", "_about": {"sends": True, "app": "Discord"}}},
            {"name": "done", "ok": True, "input": {"achieved": "Messaged Ana on Discord"}}]
    view = return_view("done", "Sent.", send, verdict=UNSEEN_SEND)
    # review F7: below likely the title is what it tried, never the brain's past-tense claim
    assert (view["icon"], view["head"], view["title"]) == ("dash", "Not confirmed", "Sending Ana the message")
    assert view["rows"][0] == {"text": "Not confirmed: the message did not show up in the chat", "icon": "dash"}
    assert not any(r["icon"] == "check" for r in view["rows"]) and view["worth"]
    ok = return_view("done", "Sent.", send, verdict=outcome_verdict([_act("press", send="confirmed")]))
    assert ok["head"] == "Done" and ok["rows"][0] == {"text": "Checked on screen: the message shows in the chat",
                                                      "icon": "check"}
    assert return_view("done", "Sent.", send)["head"] == "Done"       # no verdict: as before
    partly = outcome_verdict([_act("launch_app", app="Spotify"),
                              _act("click", verdict=False, why="x", app="Slack", what="Quitting Slack")])
    pv = return_view("done", "Done.", send[:1], verdict=partly)
    assert pv["head"] == "Partly done" and pv["rows"][0]["text"] == "Didn’t work: quitting Slack"
    closed = [{"name": "done", "ok": True, "input": {"achieved": "Closed Slack"}}]
    fv = return_view("done", "Closed Slack.", closed, verdict=outcome_verdict(
        [_act("click", verdict=False, why="x", app="Slack", what="Quitting Slack")]))
    assert (fv["head"], fv["title"]) == ("Didn’t work", "Quitting Slack")
    assert return_view("done", "Closed Slack.", closed, verdict=ok)["title"] == "Closed Slack"
    assert level_row(outcome_verdict([_read()])) is None
    assert graded_reply("Sent.", UNSEEN_SEND) == "Sent.\n\nNot confirmed: the message did not show up in the chat."
    assert graded_reply("Paused.", outcome_verdict([_act("press")])) == "Paused."


def test_the_island_flash_never_shows_the_done_chip_below_likely() -> None:
    from yuki.ui.island import FinalInfo, IslandInputs, RunInfo, spec_for

    shaky = spec_for("done-flash", IslandInputs(final=FinalInfo("Messaged Ana on Discord", level="unverified")))
    assert shaky.chip == "unconfirmed" and shaky.face == "handed" and shaky.sub.startswith("Not confirmed")
    broke = spec_for("done-flash", IslandInputs(final=FinalInfo("Closed Slack", level="failed")))
    assert broke.chip == "failed" and broke.sub.startswith("Didn't work")
    fine = spec_for("done-flash", IslandInputs(final=FinalInfo("Closed Slack", level="verified")))
    assert fine.chip == "done"
    # review F6: the compact pill and the open run card never carry the done check below likely either
    pill = spec_for("done-flash", IslandInputs(final=FinalInfo("Sending Ana the message", level="unverified")),
                    compact=True)
    assert pill.compact and pill.face == "handed"
    assert spec_for("done-flash", IslandInputs(final=FinalInfo("Closed Slack", level="verified")),
                    compact=True).face == "done-flash"
    run = RunInfo(7, 1.0)
    card = spec_for("done-flash", IslandInputs(final=FinalInfo("Quitting Slack", level="failed", run=run)),
                    expanded=True)
    assert card.exp and card.face == "handed"


def test_the_window_run_line_carries_the_level() -> None:
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "job": 4, "text": "tell sam on my way", "at": 1.0})
    m.apply({"type": "started", "job": 4, "lane": "worker", "at": 1.1})
    m.apply({"type": "achieved", "job": 4, "text": "Messaged Sam on Discord", "level": "unverified",
             "label": "Not confirmed", "seen": "the message did not show up in the chat", "at": 2.0})
    step = m.items[-1].steps[-1]
    assert step.ok is None and step.result == "Not confirmed: the message did not show up in the chat"
    m.apply({"type": "user", "job": 5, "text": "close slack", "at": 3.0})
    m.apply({"type": "started", "job": 5, "lane": "worker", "at": 3.1})
    m.apply({"type": "achieved", "job": 5, "text": "Closed Slack", "at": 4.0})
    assert m.items[-1].steps[-1].ok is True and m.items[-1].steps[-1].result == ""   # no level sent: as before


def test_review_f5_the_web_window_shows_the_level_below_likely() -> None:
    from yuki.ui import web_data
    from yuki.ui.chat_model import ChatModel

    m = ChatModel()
    m.apply({"type": "user", "job": 4, "text": "tell sam on my way", "at": 1.0})
    m.apply({"type": "started", "job": 4, "lane": "worker", "at": 1.1})
    m.apply({"type": "achieved", "job": 4, "text": "Sending Sam the message", "level": "unverified",
             "label": "Not confirmed", "seen": "the message did not show up in the chat", "at": 2.0})
    out = web_data.chat_item(m.items[-1], now=3.0)
    assert out["outcome"] == {"text": "Not confirmed: the message did not show up in the chat", "ok": None}
    m.apply({"type": "user", "job": 5, "text": "close slack", "at": 3.0})
    m.apply({"type": "started", "job": 5, "lane": "worker", "at": 3.1})
    m.apply({"type": "achieved", "job": 5, "text": "Closed Slack", "level": "verified", "label": "Checked on screen",
             "seen": "its window is gone", "at": 4.0})
    assert "outcome" not in web_data.chat_item(m.items[-1], now=5.0)          # at likely or better: nothing extra
    js = (Path(web_data.__file__).parent / "web" / "pages" / "drawer.js").read_text(encoding="utf-8")
    assert "it.outcome" in js


def test_the_talkers_action_ledger_gets_the_requests_level() -> None:
    from test_voice_v39 import Params, _settle

    async def main() -> Any:
        rt, bridge = _voice_bridge()
        bridge.speak_result = None
        params = Params("tell sam on my way")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], "Sent.", UNSEEN_SEND)
        await asyncio.wait_for(task, 1)
        return bridge

    lines = asyncio.run(main()).actions.lines()
    assert any("as a whole -> not verified: Not confirmed: the message did not show up in the chat" in ln
               for ln in lines)


def test_the_tray_settles_an_unseen_send_at_its_level(qapp: Any) -> None:
    from test_appC1_run_text import _bare_ui
    from yuki.ui.chat_model import ChatModel

    ui = _bare_ui(qapp, voice_owns=False)
    model = ChatModel()
    ui.window_host = SimpleNamespace(open_window=lambda *a: None, emit=model.apply)
    bodies: list[str] = []
    ui.cards[6] = SimpleNamespace(set_body=lambda text, **k: bodies.append(text))
    model.apply({"type": "user", "job": 6, "text": "message Ana that I'm late", "at": 1.0})
    model.apply({"type": "started", "job": 6, "lane": "worker", "at": 1.1})
    ui._on_started("worker", 6, "message Ana that I'm late")
    send = {"text": "running late", "press_enter": True, "what": "send Ana the message",
            "_about": {"sends": True, "app": "Discord", "role": "Edit", "element": "Message @Ana"}}
    ui._on_tool_called("worker", 6, "type_text", send)
    ui._on_tool_finished("worker", 6, "type_text", True, "typed, Enter pressed")
    ui._on_tool_called("worker", 6, "done", {"message": "Sent!", "about": [], "achieved": "Messaged Ana on Discord"})
    ui._on_outcome("worker", 6, UNSEEN_SEND)
    ui._on_finished("worker", 6, "Sent!")
    view = ui._return_pending["view"]
    assert view["head"] == "Not confirmed" and view["rows"][0]["icon"] == "dash"
    assert bodies == ["Sent!\n\nNot confirmed: the message did not show up in the chat."]
    last = model.items[-1].steps[-1]
    assert last.ok is None and last.result.startswith("Not confirmed")
    settle = [f for n, f in ui.events if n == "settle"][-1]
    assert settle["level"] == "unverified"


@pytest.fixture
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
