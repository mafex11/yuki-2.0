"""fix6 (report 73, '## Results, 5 Oct (release 6)'): the live recheck's four failures, rebuilt with invented data.

1. Honest wording: the closing text obeys the outcome verdict. The brain classes its own message (``done.claims``:
   checked / done / tried / answer); the claim check holds a class its steps do not back (a claim of something done
   when no action ran, "checked" above the level, "done" below likely), and below likely the user gets the verdict's
   line unless the message is what was tried.
2. Full screen: a full-screen control whose own on/off reads the same in both views is judged by the window itself
   (Windows: its size against the monitor), never "already that way" from its own state; restore on a full-screen
   window is not "restored".
3. A slider whose Value text is not its level (Spotify's volume says "0:00") shows its RangeValue as a percent.
4. Volume changes: the close-call second look (3-4 s of every 4-5 s step) runs only before a call that can do lasting
   harm; the slider's change-event wait never sits out its whole bound in one wait.

Fakes and stand-ins only: no network, no desktop, no microphone, no real memory, nothing sent.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from yuki.agent.finish import (EXPECTED_UNVERIFIABLE, LINE_FAILED, LINE_NOT_CHECKED, LINE_NOTHING_DONE, LINE_SEND_UNCONFIRMED,
                               LINE_UNVERIFIED, claim, claim_gap, honest_text, outcome_verdict)


def _act(tool: str, *, ok: bool = True, verdict: bool | None = True, why: str = "", app: str | None = "Spotify",
         **extra: Any) -> dict[str, Any]:
    return {"kind": "action", "tool": tool, "ok": ok, "verdict": verdict, "why": why, "app": app, "input": {},
            **extra}


VERIFIED = outcome_verdict([_act("click", why="something visibly changed after it")])
FAILED = outcome_verdict([_act("click", verdict=False, why="nothing visible changed in the window")])
UNSEEN = outcome_verdict([_act("press", verdict=None, why="not seen", send="unconfirmed", send_sig="ab12")])
ANSWER = outcome_verdict([])


# -- 1. the claim check and the closing text -----------------------------------------------------------------------


def test_the_brains_own_class_is_read_from_its_fixed_values_only() -> None:
    assert [claim(v) for v in ("checked", " Done ", "tried", "answer")] == ["checked", "done", "tried", "answer"]
    assert claim(None) is None and claim("confirmed") is None and claim(3) is None


def test_the_claim_check_catches_actions_that_never_ran_and_words_above_the_level() -> None:
    # run 19: no action, yet "I opened and closed the queue, confirmed"
    assert "did anything" in claim_gap("done", ANSWER)
    assert "nothing was read" in claim_gap("checked", ANSWER, read=False)
    assert claim_gap("checked", ANSWER, read=True) is None          # "I checked: it's at 40%" after a read is fine
    assert claim_gap("answer", ANSWER) is None
    # "checked" needs a step whose own check showed it; "done" needs likely at least
    assert "never checked or confirmed" in claim_gap("checked", FAILED)
    assert "never checked or confirmed" in claim_gap("checked", UNSEEN)
    likely = outcome_verdict([_act("type_text", verdict=None, why=EXPECTED_UNVERIFIABLE)])
    assert likely["level"] == "likely" and claim_gap("checked", likely) and claim_gap("done", likely) is None
    assert "did not work" in claim_gap("done", FAILED) and "could not be confirmed" in claim_gap("done", UNSEEN)
    assert claim_gap("checked", VERIFIED) is None and claim_gap("done", VERIFIED) is None
    assert claim_gap("tried", FAILED) is None and claim_gap(None, FAILED) is None


def test_below_likely_the_user_gets_the_verdicts_line_unless_the_words_are_what_was_tried() -> None:
    assert honest_text("Closed it, confirmed.", VERIFIED, "checked") == ("Closed it, confirmed.", None)
    assert honest_text("Sent.", UNSEEN, "done") == (LINE_SEND_UNCONFIRMED, "below_likely")
    assert honest_text("Sent.", UNSEEN, None) == (LINE_SEND_UNCONFIRMED, "below_likely")      # plain text, no class
    assert honest_text("It worked.", FAILED, "checked") == (LINE_FAILED, "below_likely")
    tried = "I clicked Queue, but I couldn't see it open."
    assert honest_text(tried, FAILED, "tried") == (f"{LINE_FAILED} {tried}", "below_likely_tried")
    unbacked = outcome_verdict([_act("click", verdict=None, why="could not tell")], unbacked=["a state unseen"])
    assert honest_text(tried, unbacked, "tried") == (LINE_UNVERIFIED, "below_likely")          # it tells unseen states
    # nothing done: a claim of something done or checked becomes its own fixed line; an unread answer keeps its words
    run19 = outcome_verdict([], unbacked=["it says you did something, but no action ran in this request"])
    assert honest_text("Opened the queue and closed it, confirmed.", run19, "done") == (LINE_NOTHING_DONE,
                                                                                         "nothing_done")
    assert honest_text("I checked, it's open.", run19, "checked") == (LINE_NOT_CHECKED, "not_checked")
    assert honest_text("I think it's paused.", run19, "answer") == ("I think it's paused.", None)
    assert honest_text("Spotify is at 40%.", ANSWER, "answer") == ("Spotify is at 40%.", None)


def _brain(*blocks: Any) -> Any:
    from test_d_brain import _response

    return [_response(b) for b in blocks]


def test_run_19_no_action_and_a_confirmed_claim_is_held_once_then_said_as_not_done(tmp_path: Path) -> None:
    from test_d_brain import Client, _agent, _block, _run
    from yuki.log.events import Final

    done = {"message": "Opened the queue and closed it again, confirmed.", "about": [], "states": [],
            "achieved": "Opened and closed the queue", "claims": "done"}
    client = Client(_brain(_block("tool_use", id="t1", name="done", input=done),
                           _block("tool_use", id="t2", name="done", input=done)))
    agent = _agent(client, tmp_path, judge=False)
    final = _run(agent, "open the queue and close it")[-1]
    assert isinstance(final, Final) and final.text == LINE_NOTHING_DONE
    assert final.outcome["level"] == "unverified" and not final.outcome["acted"]
    held = [m for m in client.brain_calls[1]["messages"][-1]["content"] if isinstance(m, dict)
            and "did anything" in str(m.get("text") or m.get("content") or "")]
    assert held, "the claim check sent the done back once with its reason"


def test_an_unseen_send_claimed_as_done_is_held_then_said_as_the_verdicts_line(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _run, _send_memory
    from yuki.log.events import Final

    sends = lambda p: {"decision": "allow", "sends": True, "scope": "call", "why": "dictated"}  # noqa: E731
    done = {"message": "Sent it, confirmed.", "about": [], "states": [], "achieved": "Messaged Sam",
            "claims": "checked"}
    client = Client(_brain(_block("tool_use", id="t0", name="check_situation",
                                  input={"action": "send the message", "hwnd": CHROME}),
                           _block("tool_use", id="t1", name="type_text",
                                  input={"text": "on my way", "press_enter": True, "expect_hwnd": CHROME}),
                           _block("tool_use", id="t2", name="done", input=done),
                           _block("tool_use", id="t3", name="done", input=done)), judge=sends)
    agent = _agent(client, tmp_path, memory=_send_memory([]))
    final = _run(agent, "tell sam on my way")[-1]
    assert isinstance(final, Final) and final.text == LINE_SEND_UNCONFIRMED
    assert final.outcome["level"] == "unverified"
    # the same request worded as what was tried keeps its words after the verdict's line (review: the line leads)
    tried = {**done, "message": "I sent it to Sam, but I couldn't confirm it arrived.", "claims": "tried"}
    client = Client(_brain(_block("tool_use", id="t0", name="check_situation",
                                  input={"action": "send the message", "hwnd": CHROME}),
                           _block("tool_use", id="t1", name="type_text",
                                  input={"text": "on my way", "press_enter": True, "expect_hwnd": CHROME}),
                           _block("tool_use", id="t2", name="done", input=tried)), judge=sends)
    agent = _agent(client, tmp_path, memory=_send_memory([]))
    final = _run(agent, "tell sam on my way")[-1]
    assert final.text == f"{LINE_SEND_UNCONFIRMED} {tried['message']}" and final.outcome["level"] == "unverified"


def test_a_verified_request_keeps_its_words_and_is_not_held(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _run
    from yuki.log.events import Final

    done = {"message": "Chrome's minimized, I checked.", "about": [CHROME], "states": [], "achieved": "Minimized "
            "Chrome", "claims": "checked"}
    client = Client(_brain(_block("tool_use", id="t1", name="window_action", input={"hwnd": CHROME,
                                                                                     "action": "minimize"}),
                           _block("tool_use", id="t2", name="done", input=done)))
    agent = _agent(client, tmp_path, judge=False)
    final = _run(agent, "minimize chrome")[-1]
    assert isinstance(final, Final) and final.text == done["message"] and final.outcome["level"] == "verified"
    assert len(client.brain_calls) == 2


def test_the_done_schema_requires_the_class_with_worked_examples_and_the_budget_holds() -> None:
    import json

    from yuki.agent.tools import CLAIMS_FIELD, TOOLS_BY_NAME, tool_params

    schema = TOOLS_BY_NAME["done"]["input_schema"]
    assert CLAIMS_FIELD in schema["required"]
    prop = schema["properties"][CLAIMS_FIELD]
    assert prop["enum"] == ["checked", "done", "tried", "answer"] and "Closed Slack" in prop["description"]
    assert len(json.dumps(tool_params(cacheable=False, screenshot_policy="never"))) < 39_000


def test_the_talkers_notes_never_allow_checked_or_confirmed_below_likely() -> None:
    from yuki.voice.mouth import result_payload

    for verdict in (UNSEEN, FAILED, outcome_verdict([], unbacked=["x"])):
        note = result_payload("done", "Done, confirmed.", outcome=verdict)["how_to_say_it"]
        assert "confirmed" in note and ("checked or confirmed" in note or "checked, or confirmed" in note
                                        or "did, checked or confirmed" in note)


def test_the_owed_line_is_not_doubled_when_the_claim_was_already_replaced() -> None:
    from test_honest_outcome import _end, _voice_bridge
    from test_voice_v39 import Params, _settle
    from yuki.voice.narrator import OwedLedger

    run19 = outcome_verdict([], unbacked=["it says you did something, but no action ran in this request"])

    async def main() -> Any:
        rt, bridge = _voice_bridge()
        bridge.ledger = OwedLedger()
        params = Params("open the queue and close it")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        await _end(rt, rt.submitted[0][0], LINE_NOTHING_DONE, run19)
        await asyncio.wait_for(task, 1)
        return params, bridge

    params, bridge = asyncio.run(main())
    assert bridge.ledger.pending[-1].text == LINE_NOTHING_DONE
    assert params.results[-1][0]["outcome"] == "not checked"


# -- 2. full screen judged by the window -----------------------------------------------------------------------------


class _FullScreenDesk:
    """Spotify's live shape: one toggle Button named "Enter full screen" in the normal view and "Exit full screen" in
    the full-screen view, off in both (its own ToggleState never changes), with one RuntimeId; the window's own
    full screen is what Windows measures (``window_state``)."""

    def __new__(cls, *, full: bool = False) -> Any:
        from yuki.voice.offline import ChatPlayerDesk

        class Desk(ChatPlayerDesk):
            def __init__(self) -> None:
                super().__init__()
                self.full = full
                self.full_clicks = 0

            def window_state(self, hwnd: int) -> str | None:
                return ("fullscreen" if self.full else "normal") if int(hwnd) == self.PLAYER else "normal"

            def get_window_tree(self, hwnd: int, **kw: Any) -> dict[str, Any]:
                tree = super().get_window_tree(hwnd, **kw)
                for e in tree["elements"]:
                    if e["name"] == "Enter Full screen":
                        e.update(name="Exit full screen" if self.full else "Enter full screen", states=["off"],
                                 runtime_id=(42, 7, 1))
                return tree

            def click(self, x: int, y: int, **kwargs: Any) -> Any:
                result = super().click(x, y, **kwargs)
                if self.clicks and self.clicks[-1] in ("Enter full screen", "Exit full screen"):
                    self.toggles["Enter Full screen"] = False      # its own state never changes
                    self.full = not self.full
                    self.full_clicks += 1
                return result

        return Desk()


def _full_ids(desk: Any) -> dict[str, int]:
    return {e["name"]: e["id"] for e in desk.get_window_tree(desk.PLAYER)["elements"]}


def test_leave_full_screen_is_judged_by_the_window_never_already_off_from_the_button() -> None:
    from yuki.agent.tools import Dispatcher

    desk = _FullScreenDesk(full=True)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    button = _full_ids(desk)["Exit full screen"]
    out = d.dispatch("click", {"element": button, "expect_hwnd": desk.PLAYER, "toggle_to": "not_full_screen"})
    assert out.ok and desk.full is False and desk.full_clicks == 1
    details = out.payload["details"]
    assert details["verified"] is True and not details.get("noop") and details["window_state_after"] == "normal"
    assert "measured from Windows" in out.summary
    # asked again: the window is already not full screen, measured: not clicked, verified by the window
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    again = d.dispatch("click", {"element": _full_ids(desk)["Enter full screen"], "expect_hwnd": desk.PLAYER,
                                 "toggle_to": "not_full_screen"})
    assert again.ok and again.payload["details"]["noop"] and desk.full_clicks == 1
    assert "already not full screen" in again.summary


def test_a_plain_on_click_that_flips_the_window_is_verified_by_it_and_learned_for_the_next_off() -> None:
    from yuki.agent.tools import Dispatcher

    desk = _FullScreenDesk(full=False)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    on = d.dispatch("click", {"element": _full_ids(desk)["Enter full screen"], "expect_hwnd": desk.PLAYER,
                              "toggle_to": "on"})
    assert on.ok and desk.full and on.payload["details"]["verified"] is True
    assert "switches full screen" in on.summary
    # live 5 Oct: "leave full screen" on the off-reading button was "already that way", verified, still full screen
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    off = d.dispatch("click", {"element": _full_ids(desk)["Exit full screen"], "expect_hwnd": desk.PLAYER,
                               "toggle_to": "off"})
    assert off.ok and not off.payload["details"].get("noop") and desk.full is False
    assert off.payload["details"]["verified"] is True and desk.full_clicks == 2


def test_a_toggle_read_under_another_name_with_the_same_state_is_never_already() -> None:
    from yuki.agent.tools import Dispatcher, ToolError

    desk = _FullScreenDesk(full=False)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})      # read as "Enter full screen", off
    button = _full_ids(desk)["Enter full screen"]
    desk.full = True                                          # the user went full screen themselves
    out = d.dispatch("click", {"element": button, "expect_hwnd": desk.PLAYER, "toggle_to": "off"})
    assert not out.ok and "name changed since your read" in out.summary and desk.full_clicks == 0
    assert isinstance(ToolError("x"), Exception)


def test_restore_on_a_full_screen_window_is_not_reported_restored() -> None:
    from yuki.actions import ActionResult
    from yuki.agent.tools import Dispatcher

    class Desk:
        def window_action(self, hwnd: int, action: str) -> Any:
            return ActionResult(ok=True, summary="Spotify.exe \"Spotify\" is already restored",
                                details={"hwnd": hwnd, "action": action, "window_state_before": "fullscreen",
                                         "window_state_after": "fullscreen", "window_state_changed": False},
                                elapsed_ms=1.0)

    out = Dispatcher(Desk()).dispatch("window_action", {"hwnd": 0x3002, "action": "restore"})
    assert not out.ok and "still full screen" in out.summary
    assert out.payload["details"]["verified"] is False


# -- 3. the slider's level when its text is not one ----------------------------------------------------------------


def test_spotifys_volume_text_0_00_no_longer_hides_its_level(monkeypatch: pytest.MonkeyPatch) -> None:
    from _fake_uia import player_window, read
    from yuki.perception import tree as T

    window = player_window(volume=0.4)
    stack = [window]
    while stack:
        node = stack.pop()
        if node.role == "Slider" and node.name == "Change volume":
            node.value = "0:00"
        stack.extend(getattr(node, "children", []) or [])
    tree = read(window, monkeypatch=monkeypatch)
    slider = next(e for e in tree.elements if e.role == "Slider" and e.name == "Change volume")
    assert slider.value == '40% (0.4, range 0-1; its text: "0:00")' and slider.range == (0.4, 0.0, 1.0)
    line = next(ln for ln in T.format_window_tree(tree).splitlines() if '"Change volume"' in ln and "Slider" in ln)
    assert "40%" in line


def test_a_value_text_that_is_the_level_stays_and_numbers_get_a_percent() -> None:
    from yuki.perception import tree as T

    numbers = (0.4, 0.0, 1.0)
    assert T._is_level("40%", numbers) and T._is_level("0.4", numbers) and T._is_level(" 40 ", numbers)
    assert not T._is_level("0:00", numbers) and not T._is_level("Volume 40", numbers)
    assert not T._is_level("4", numbers)
    assert T._is_level("37", (37.0, 0.0, 100.0)) and T._percent_words(1.0, 0.0, 3.0) == "33.3%"

    def props(value: float, low: float, high: float) -> Any:
        table = {T._P_IS_RANGE_VALUE_AVAILABLE: True, T._P_RANGE_VALUE_IS_READONLY: False,
                 T._P_RANGE_VALUE_VALUE: value, T._P_RANGE_VALUE_MINIMUM: low, T._P_RANGE_VALUE_MAXIMUM: high}
        return table.get

    assert T._slider_value(props(0.4, 0.0, 1.0)) == "0.4 (40%, range 0-1)"
    assert T._slider_value(props(37, 0, 100)) == "37 (range 0-100)"
    assert T._slider_value(props(0.4, 0.0, 1.0), "40%") == "40%"
    assert T._slider_value(props(73, 0, 216), "1:13") == '33.8% (73, range 0-216; its text: "1:13")'


# -- 4. volume change timing -------------------------------------------------------------------------------------------


def test_the_close_call_second_look_waits_only_before_a_call_that_can_do_lasting_harm() -> None:
    from yuki.agent.loop import _close_call_matters

    assert not _close_call_matters("set_slider", {"element": 5, "by": -10}, {"role": "Text"})
    assert not _close_call_matters("scroll", {"dy": -3}, {})
    assert not _close_call_matters("window_action", {"hwnd": 9, "action": "maximize"}, {})
    assert _close_call_matters("window_action", {"hwnd": 9, "action": "close"}, {})
    assert _close_call_matters("type_text", {"text": "the address"}, {})
    assert _close_call_matters("click", {"element": 3}, {"role": "Button"})
    assert _close_call_matters("press", {"key": "enter"}, {})


def test_a_close_call_does_not_hold_a_level_change_for_a_second_look_but_does_a_close(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.agent.loop as loop_mod
    from test_d_brain import CHROME, Client, _agent, _block, _run

    monkeypatch.setattr(loop_mod, "_close_call", lambda frame: True)   # the frame calls two things a close call

    def run(action: str) -> Client:
        client = Client(_brain(_block("tool_use", id="t0", name="check_situation",
                                      input={"action": f"{action} Chrome's window", "hwnd": CHROME}),
                               _block("tool_use", id="t1", name="window_action",
                                      input={"hwnd": CHROME, "action": action,
                                             "risk": {"reversible": True, "user_asked": True}}),
                               _block("tool_use", id="t2", name="done",
                                      input={"message": "Done.", "about": [], "states": [],
                                             "achieved": "Changed Chrome's window", "claims": "done"})),
                        advice=[{"verdict": "proceed", "blocking": False, "advice": "fine"}] * 3)
        _run(_agent(client, tmp_path, judge=False, consult=True), f"{action} it")
        return client

    assert run("maximize").consult_calls == []
    assert len(run("close").consult_calls) == 1


def test_a_set_value_that_arrives_without_its_event_is_read_within_a_slice_not_the_whole_bound() -> None:
    from yuki.actions import slider as S

    class Watch:
        live = True

        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait(self, t: float) -> None:
            self.waits.append(t)
            time.sleep(min(t, 0.005))

        def close(self) -> None:
            pass

    class Slider:
        def __init__(self) -> None:
            self.value, self.reads, self.pending = 0.4, 0, None
            self.w = Watch()

        def watch(self) -> Watch:
            return self.w

        def read(self) -> tuple[float, float, float, bool, float]:
            self.reads += 1
            if self.pending is not None and self.reads >= 4:
                self.value, self.pending = self.pending, None   # it arrives, with no event
            return self.value, 0.0, 1.0, False, 0.01

        def set(self, value: float) -> None:
            self.pending = value

        def focus(self) -> None:
            pass

    s = Slider()
    started = time.monotonic()
    ok, summary, details = S.drive(s, to=30, hold_s=0.0)
    assert ok and details["verified"] and details["after_percent"] == 30.0
    assert time.monotonic() - started < 1.0
    assert s.w.waits and max(s.w.waits) <= S.EVENT_SLICE_S


# -- review fixes (F1-F6, items not done) ------------------------------------------------------------------------------


def _todo_memory() -> Any:
    """Memory whose writes succeed (a to-do added), nothing else."""
    from yuki.agent.memory import MemoryAccess
    from yuki.agent.tools import ToolOutcome

    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: (_ for _ in ()).throw(RuntimeError("x")))
            self.ran: list[str] = []

        @property
        def installed(self) -> bool:
            return True

        def run_tool(self, name: str, tool_input: dict[str, Any], *, request: str | None = None) -> Any:
            self.ran.append(name)
            return ToolOutcome(name=name, ok=True, summary="Added to your list.",
                               content=[{"type": "text", "text": "Added to your list."}], payload={"saved": True})

        def call_method(self, name: str, *args: Any, timeout_s: float, **kwargs: Any) -> Any:
            return None

        def context(self, **kw: Any) -> Any:
            raise RuntimeError("no memory context in this test")

    return Memory()


def test_f1_a_memory_or_todo_write_backs_a_done_claim_and_is_never_nothing_done() -> None:
    from yuki.agent.finish import did_other

    todo = {"kind": "memory", "tool": "todo", "ok": True, "input": {"action": "add", "text": "buy milk"}}
    listed = {"kind": "memory", "tool": "todo", "ok": True, "input": {"action": "list"}}
    rule = {"kind": "memory", "tool": "remember_rule", "ok": True, "input": {"rule": "x"}}
    assert did_other([todo]) and did_other([rule]) and not did_other([listed])
    assert not did_other([{**todo, "ok": False}])
    added = outcome_verdict([todo])
    assert added["did"] and not added["acted"]
    assert claim_gap("done", added) is None
    assert honest_text("Added buy milk to your list.", outcome_verdict([todo], unbacked=["x"]), "done") == (
        "Added buy milk to your list.", None)
    assert "do it now" not in claim_gap("done", ANSWER)


def test_f1_add_a_todo_then_done_is_not_held_or_repeated(tmp_path: Path) -> None:
    from test_d_brain import Client, _agent, _block, _run
    from yuki.log.events import Final

    done = {"message": "Added buy milk to your list.", "about": [], "states": [], "achieved": "Added buy milk",
            "claims": "done"}
    client = Client(_brain(_block("tool_use", id="t1", name="todo", input={"action": "add", "text": "buy milk"}),
                           _block("tool_use", id="t2", name="done", input=done),
                           _block("tool_use", id="t3", name="todo", input={"action": "add", "text": "buy milk"}),
                           _block("tool_use", id="t4", name="done", input=done)))
    memory = _todo_memory()
    final = _run(_agent(client, tmp_path, judge=False, memory=memory), "add buy milk to my todo list")[-1]
    assert isinstance(final, Final) and final.text == done["message"]
    assert memory.ran.count("todo") == 1 and len(client.brain_calls) == 2


def test_f2_a_close_call_consults_on_the_close_after_a_selecting_focus(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.agent.loop as loop_mod
    from test_d_brain import CHROME, Client, _agent, _block, _run

    monkeypatch.setattr(loop_mod, "_close_call", lambda frame: True)
    client = Client(_brain(_block("tool_use", id="t0", name="check_situation",
                                  input={"action": "close Chrome's window", "hwnd": CHROME}),
                           _block("tool_use", id="t1", name="focus_window", input={"hwnd": CHROME}),
                           _block("tool_use", id="t2", name="window_action",
                                  input={"hwnd": CHROME, "action": "close",
                                         "risk": {"reversible": True, "user_asked": True}}),
                           _block("tool_use", id="t3", name="done",
                                  input={"message": "Closed it.", "about": [], "states": [],
                                         "achieved": "Closed Chrome", "claims": "done"})),
                    advice=[{"verdict": "proceed", "blocking": False, "advice": "fine"}] * 3)
    _run(_agent(client, tmp_path, judge=False, consult=True), "close it")
    assert len(client.consult_calls) == 1


def test_f3_checked_after_reading_the_window_back_stands_at_likely() -> None:
    from yuki.agent.finish import read_back

    scroll = _act("scroll", verdict=None, why=EXPECTED_UNVERIFIABLE)
    look = {"kind": "read", "tool": "look_at_window", "ok": True, "app": "Spotify", "input": {}}
    elsewhere = {**look, "app": "Discord"}
    v = outcome_verdict([scroll, look])
    assert v["level"] == "likely" and read_back([scroll, look])
    assert claim_gap("checked", v, read=True, looked_back=True) is None
    assert not read_back([look, scroll]) and not read_back([scroll, elsewhere])
    gap = claim_gap("checked", v, read=True, looked_back=False)
    assert gap and "read the window back now" in gap
    # below likely a read-back never makes "checked" right, and the gap does not ask for a look
    gap = claim_gap("checked", FAILED, read=True, looked_back=True)
    assert gap and "read the window back" not in gap and "never checked or confirmed" in gap


def test_f3_a_claims_only_hold_does_not_tell_it_to_look_first(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _run, _texts

    done = {"message": "It is minimized, I checked.", "about": [], "states": [], "achieved": "Minimized Chrome"}
    client = Client(_brain(_block("tool_use", id="t1", name="window_action", input={"hwnd": CHROME,
                                                                                     "action": "minimize"}),
                           _block("tool_use", id="t2", name="done", input={**done, "claims": "checked"}),
                           _block("tool_use", id="t3", name="done", input={**done, "claims": "done"})))
    agent = _agent(client, tmp_path, judge=False)
    # the run's own check stands in for one whose level is below verified (the gap's words come from claim_gap)
    agent._claim_gap = lambda claims: (None if claims != "checked" else "it says you checked or confirmed it, but "
                                       "no step's own check showed it; say what you did")
    _run(agent, "minimize chrome")
    note = _texts(client.brain_calls[2]["messages"][-1]["content"])
    assert "it says you checked" in note and "Look first" not in note


def test_f4_a_fresh_off_on_the_full_screen_button_is_never_verified_already_while_the_window_is_full() -> None:
    from yuki.agent.tools import Dispatcher

    desk = _FullScreenDesk(full=True)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    out = d.dispatch("click", {"element": _full_ids(desk)["Exit full screen"], "expect_hwnd": desk.PLAYER,
                               "toggle_to": "off"})
    details = out.payload["details"]
    assert out.ok and details.get("noop") and details.get("toggle_unconfirmed") and "verified" not in details
    assert "the window itself is full screen" in out.summary and desk.full and desk.full_clicks == 0
    # the note's way: judged by the window, it clicks and is verified by it
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    out = d.dispatch("click", {"element": _full_ids(desk)["Exit full screen"], "expect_hwnd": desk.PLAYER,
                               "toggle_to": "not_full_screen"})
    assert out.ok and out.payload["details"]["verified"] is True and desk.full is False


def test_f5_restoring_a_minimized_full_screen_game_is_restored_and_a_still_full_window_is_not() -> None:
    from yuki.actions import ActionResult
    from yuki.agent.tools import Dispatcher

    class Desk:
        before = "minimized"

        def window_action(self, hwnd: int, action: str) -> Any:
            return ActionResult(ok=True, summary="Game.exe \"Game\" is now restored",
                                details={"hwnd": hwnd, "action": action, "window_state_before": self.before,
                                         "was_minimized": self.before == "minimized",
                                         "window_state_after": "fullscreen", "window_state_changed": True},
                                elapsed_ms=1.0)

    desk = Desk()
    out = Dispatcher(desk).dispatch("window_action", {"hwnd": 0x3002, "action": "restore"})
    assert out.ok and "still full screen" not in out.summary
    desk.before = "fullscreen"
    out = Dispatcher(desk).dispatch("window_action", {"hwnd": 0x3002, "action": "restore"})
    assert not out.ok and out.payload.get("ok", False) is False


def test_f6_a_name_that_changed_between_reads_is_not_learned_for_the_session() -> None:
    from yuki.agent.tools import Dispatcher

    desk = _FullScreenDesk(full=False)
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})       # "Enter full screen", off
    button = _full_ids(desk)["Enter full screen"]
    desk.full = True                                           # its name changes under it (a live count, a view)
    held = d.dispatch("click", {"element": button, "expect_hwnd": desk.PLAYER, "toggle_to": "off"})
    assert not held.ok and not d._untracked_toggles
    desk.full = False                                          # back as it was read; a fresh read agrees
    d.dispatch("look_at_window", {"hwnd": desk.PLAYER})
    again = d.dispatch("click", {"element": _full_ids(desk)["Enter full screen"], "expect_hwnd": desk.PLAYER,
                                 "toggle_to": "off"})
    assert again.ok and again.payload["details"].get("noop") and again.payload["details"]["verified"] is True


def _response_text(text: str) -> Any:
    from test_d_brain import _block, _response

    return _response(_block("text", text=text), stop="end_turn")


def test_a_plain_text_reply_after_steps_is_sent_back_once_for_done(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _run, _texts
    from yuki.log.events import Final

    client = Client([_brain(_block("tool_use", id="t1", name="look_at_window", input={"hwnd": CHROME}))[0],
                     _response_text("Opened the queue and closed it, confirmed."),
                     _brain(_block("tool_use", id="t2", name="done",
                                   input={"message": "Chrome shows a page.", "about": [CHROME], "states": [],
                                          "achieved": "Found what Chrome shows", "claims": "answer"}))[0]])
    final = _run(_agent(client, tmp_path, judge=False), "what's on chrome")[-1]
    assert isinstance(final, Final) and final.text == "Chrome shows a page."
    assert "not plain text" in _texts(client.brain_calls[2]["messages"][-1]["content"])
    # a reply with no steps at all is a plain answer: not held
    client = Client([_response_text("Hi.")])
    final = _run(_agent(client, tmp_path, judge=False), "hello")[-1]
    assert isinstance(final, Final) and final.text == "Hi." and len(client.brain_calls) == 1
