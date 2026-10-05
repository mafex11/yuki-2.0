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
                  "4 s: not confirmed", app="WhatsApp", send="unconfirmed", what="Sending the message")]
    v = outcome_verdict(steps)
    assert (v["level"], v["send"], v["line"]) == ("unverified", "unconfirmed", LINE_SEND_UNCONFIRMED)
    assert v["seen"] == "the message did not show up in the chat" and v["step"] == "Sending the message"
    assert not sayable(v)
    # a later verified click in the same app never puts an unseen send right; only a confirmed send does
    assert outcome_verdict(steps + [_act("click", app="WhatsApp")])["level"] == "unverified"
    seen = outcome_verdict(steps + [_act("press", app="WhatsApp", send="confirmed")])
    assert (seen["level"], seen["send"], seen["seen"]) == ("verified", "confirmed", "the message shows in the chat")


def test_the_live_discord_send_after_failed_typing_did_not_work() -> None:
    # the typing failed, then Enter went in with nothing seen in the chat
    v = outcome_verdict([_act("type_text", ok=False, verdict=False, why="it failed", what="Typing the message"),
                         _act("press", verdict=None, why="not confirmed", send="unconfirmed")])
    assert v["level"] == "failed" and v["line"] == LINE_FAILED and not v["partial"] and v["step"] == "Typing the message"


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
    fixed = outcome_verdict([key, _act("window_action", why="its report reads back the new state", app="Warp")])
    assert fixed["level"] == "verified" and fixed["open"] == 0
    # a later verified step in another app does not put it right
    assert outcome_verdict([key, _act("launch_app", app="Spotify")])["level"] == "unverified"
    # a retry of the same tool in the same app stands for it, whatever its own level
    retry = outcome_verdict([_act("window_action", ok=False, verdict=False, why="it failed", app="Slack"),
                             _act("window_action", verdict=None, why="x", app="Slack")])
    assert retry["level"] == "unverified"


def test_part_done_part_failed_is_partly_done_and_a_verified_finishing_action_ends_it() -> None:
    steps = [_act("launch_app", app="Spotify"),
             _act("click", verdict=False, why="focus, window, its state and page were watched and nothing changed",
                  app="Slack", what="Quitting Slack")]
    v = outcome_verdict(steps)
    assert (v["level"], v["partial"], v["line"], v["step"]) == ("failed", True, LINE_PARTIAL, "Quitting Slack")
    # replayed 5 Oct: a slider that failed, then the mixer level the brain marked as finishing, verified
    tries = [_act("set_slider", ok=False, verdict=False, why="it failed", app="Spotify"),
             _act("app_volume", why="its report says it was seen", app=None)]
    assert outcome_verdict(tries, finished_by_action=True)["level"] == "verified"


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
    assert isinstance(final, Final) and final.text == "Sent."                # the brain's words, unchanged
    assert (final.outcome["level"], final.outcome["send"]) == ("unverified", "unconfirmed")
    assert final.outcome["line"] == LINE_SEND_UNCONFIRMED


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
    assert (view["icon"], view["head"], view["title"]) == ("dash", "Not confirmed", "Messaged Ana on Discord")
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
    assert level_row(outcome_verdict([_read()])) is None
    assert graded_reply("Sent.", UNSEEN_SEND) == "Sent.\n\nNot confirmed: the message did not show up in the chat."
    assert graded_reply("Paused.", outcome_verdict([_act("press")])) == "Paused."


def test_the_island_flash_never_shows_the_done_chip_below_likely() -> None:
    from yuki.ui.island import FinalInfo, IslandInputs, spec_for

    shaky = spec_for("done-flash", IslandInputs(final=FinalInfo("Messaged Ana on Discord", level="unverified")))
    assert shaky.chip == "unconfirmed" and shaky.face == "handed" and shaky.sub.startswith("Not confirmed")
    broke = spec_for("done-flash", IslandInputs(final=FinalInfo("Closed Slack", level="failed")))
    assert broke.chip == "failed" and broke.sub.startswith("Didn't work")
    fine = spec_for("done-flash", IslandInputs(final=FinalInfo("Closed Slack", level="verified")))
    assert fine.chip == "done"


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
