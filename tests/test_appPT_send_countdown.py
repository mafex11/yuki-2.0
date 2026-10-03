"""appPT (3 Oct): the send countdown, "Working · step N" with no total, and the brain's own live reasoning line.

The user's three asks, decided in CHAT-2026-10-02-revamp.md §6 (~19:45-20:55):

1. a plain "send X to Y" that passed the situation check and the judge shows who / where / the exact text with
   "3 s to stop": Stop or Esc cancels it (nothing sent, said plainly), a tapped chip swaps person or place and the
   countdown starts again, the end of the countdown sends; never for drafts or after the user answered a question;
2. no "of N" anywhere: "Working · step 2", segments that are the steps so far, a run card that never says "0/5";
3. the thinking / working line is the brain's own one-line note (its ``now`` on a step) or the situation check's plain
   line, never a canned "Thinking…", never a tool name.

Offline only: scripted brains, the stub chat desk, invented names and messages (ARCHITECTURE.md rule 7). No microphone,
speaker, live desktop or real memory.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import AskUser, ErrorEvent, Final, Partial, SendPending, SessionLogger, ToolCall, ToolResult
from yuki.voice.offline import ChatPlayerDesk

CHAT = ChatPlayerDesk.CHAT
NOW = time.time()


@pytest.fixture(autouse=True)
def _no_live_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brain's per-turn Now picture comes from memory's live service; here it is nothing (never the real screen)."""
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)


# -- a scripted brain and the stub chat (the same shapes as tests/test_vfix2.py) -------------------------------------


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _use(tool: str, i: int = 0, /, **inp: Any) -> SimpleNamespace:
    return _block("tool_use", id=f"t{tool}{i}{len(inp)}", name=tool, input=inp)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=_usage(), id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class _JudgeStream:
    def __init__(self, verdict: dict[str, Any], tool: str) -> None:
        self.verdict, self.tool = verdict, tool

    def __enter__(self) -> "_JudgeStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_usage()))
        text = json.dumps(self.verdict)
        for i in range(0, len(text), 9):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 9]))
        yield SimpleNamespace(type="message_stop")

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[_block("tool_use", name=self.tool, input=self.verdict)], usage=_usage())


def _judge(params: dict) -> dict:
    sends = "press_enter" in json.dumps(params.get("messages") or [], default=str)
    return {"decision": "allow", "question": "", "sends": sends, "scope": "call", "why": "they asked for it"}


class Brain:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        step = self.script.pop(0)
        return step(params) if callable(step) else step

    def stream(self, **params: Any) -> _JudgeStream:
        tool = params["tool_choice"]["name"]
        verdict = dict(_judge(params))
        if tool == "review":
            verdict = {"asked": "x", "toward": True, "lasting": True, "asked_effect": True, **verdict}
        return _JudgeStream(verdict, tool)


def _texts(params: dict) -> str:
    out = []
    for m in params.get("messages") or []:
        content = m.get("content")
        for b in content if isinstance(content, list) else [content]:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_result":
                    out += [str(c.get("text") or "") for c in b.get("content") or [] if isinstance(c, dict)]
            elif isinstance(b, str):
                out.append(b)
    return "\n".join(out)


class FakeMemoryClient:
    """Memory as the situation check calls it: Sam (invented), his Discord DM, nothing like the message sent yet."""

    def __init__(self, own: list[dict[str, Any]] | None = None) -> None:
        self.own = list(own or [])
        self.person = {"name": "Sam", "aka": ["sam", "sam_99"], "relation": "friend", "routes": [
            {"label": "Discord DM @sam_99", "app": "Discord.exe", "thread_ids": [3], "last_mine": NOW - 3 * 3600,
             "last_theirs": NOW - 3000, "samples": []}]}

    def contact(self, ref: Any, near: Any = None) -> dict | None:
        if str(ref).casefold() in {"sam", "sam_99"}:
            return {"found": True, **self.person}
        return {"found": False, "people": []}

    def sent_messages(self, app: Any, since: float, *, limit: int = 12) -> list[dict]:
        return [m for m in self.own if float(m["at"]) >= since]

    def recall(self, query: str, **kw: Any) -> list[dict]:
        return []


def _desk() -> ChatPlayerDesk:
    return ChatPlayerDesk(friend="Sam", handle="sam_99", history=[("Sam", NOW - 3000, "you around later?")])


def _agent(brain: Brain, tmp: Path, desk: Any, *, countdown: float = 0.3, memory: Any = None):
    from yuki.agent.judge import PolicyReader
    from yuki.agent.loop import Agent

    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv", judge=True,
                        consult=False, send_confirm_s=0.2)
    mem = memory or MemoryAccess(open_client=lambda: FakeMemoryClient())
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=brain, backend=desk,
                  prewarm=False, record_requests=False, memory=mem, lane="worker")
    agent.authorizer.policy = PolicyReader(tmp / "autonomy.json")
    agent.send_countdown_s = countdown
    return agent


def _run(agent: Any, words: str, *, on_send: Any = None, answers: list[str] | None = None) -> list[Any]:
    events, answers = [], list(answers or [])
    for ev in agent.run(words):
        events.append(ev)
        if isinstance(ev, AskUser):
            agent.answer(answers.pop(0) if answers else "no")
        if isinstance(ev, SendPending) and ev.state == "counting" and on_send is not None:
            on_send(agent, ev)
    return events


TEXT = "omw, 10 min"


def _check(i: int = 0, about: str = "Sam", **extra: Any) -> SimpleNamespace:
    return _response(_use("check_situation", i, action=f"send {about}: {TEXT}", hwnd=CHAT, about=about, **extra))


def _send(i: int = 0, **extra: Any) -> SimpleNamespace:
    return _use("type_text", i, text=TEXT, press_enter=True, expect_hwnd=CHAT, **extra)


def _done(i: int = 0) -> SimpleNamespace:
    return _use("done", i, message="Told Sam you're on your way.", about=[], states=[],
                achieved="Messaged Sam on Discord")


def _sends(events: list[Any]) -> list[SendPending]:
    return [e for e in events if isinstance(e, SendPending)]


# -- 1. the send countdown ------------------------------------------------------------------------------------------


def test_a_plain_send_waits_its_countdown_then_goes(tmp_path: Path) -> None:
    desk = _desk()
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, countdown=0.3)
    started = time.monotonic()
    events = _run(agent, "tell sam I'm on my way, 10 minutes")
    took = time.monotonic() - started
    pend = _sends(events)
    assert [p.state for p in pend] == ["counting", "going"]
    first = pend[0]
    assert first.who == "Sam" and first.app == "Discord" and first.text == TEXT and not first.typed
    assert first.seconds == pytest.approx(0.3)
    assert desk.sent == [TEXT] and took >= 0.3                     # it waited the countdown, then sent once
    order = [type(e).__name__ for e in events]
    call = next(i for i, e in enumerate(events) if isinstance(e, ToolCall) and e.name == "type_text")
    assert order.index("SendPending") > call                       # after its step is shown, before it runs
    assert any(isinstance(e, Final) for e in events)


def test_stop_in_the_countdown_cancels_the_send_and_nothing_is_typed(tmp_path: Path) -> None:
    desk = _desk()
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, countdown=5.0)

    def stop_soon(a: Any, _ev: SendPending) -> None:   # Stop on the card, from the GUI thread, mid-countdown
        threading.Timer(0.1, a.cancel).start()

    started = time.monotonic()
    events = _run(agent, "tell sam I'm on my way, 10 minutes", on_send=stop_soon)
    assert time.monotonic() - started < 3.0                        # the wait woke on the stop, not the 5 s timeout
    assert [p.state for p in _sends(events)] == ["counting", "stopped"]
    assert desk.sent == [] and TEXT not in json.dumps(desk.history)
    held = [e for e in events if isinstance(e, ToolResult) and e.name == "type_text"]
    assert held and not held[0].ok and "nothing was sent" in held[0].summary
    assert isinstance(events[-1], ErrorEvent)                      # the run ends as cancelled (the UI: "Stopped")


def test_a_tapped_chip_swaps_the_person_and_the_countdown_starts_again(tmp_path: Path, monkeypatch) -> None:
    from yuki.agent import frame as task_frame
    from yuki.agent.frame import Candidate, TaskFrame

    real = task_frame.build

    def build(*a: Any, **k: Any) -> TaskFrame:          # the resolver had Sam first, Maya close behind (invented)
        frame = real(*a, **k)
        frame.persons = [{"id": 1, "name": "Sam", "routes": [
            {"label": "Discord DM @sam_99", "app": "Discord.exe"}, {"label": "WhatsApp", "app": "WhatsApp.exe"}]}]
        frame.people = [Candidate("person", "Sam", "said_user", "you said", 5.0, 1.0),
                        Candidate("person", "Maya", "recent", "a chat 4 min ago", 240.0, 0.8)]
        return frame

    monkeypatch.setattr(task_frame, "build", build)
    desk = _desk()
    seen: dict[str, str] = {}

    def after_swap(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _check(1, about="Maya")

    brain = Brain([_check(), _response(_send()), after_swap, _response(_send(1), _done()), _response(_done(2))])
    agent = _agent(brain, tmp_path, desk, countdown=0.6)
    taps: list[bool] = []

    def tap_once(a: Any, ev: SendPending) -> None:
        if not taps:
            assert ev.who_alts == ("Maya",) and ev.where_alts == ("WhatsApp",) and ev.from_memory
            assert ev.where == "Discord DM @sam_99"
            threading.Timer(0.05, lambda: taps.append(a.send_choice("who", "Maya"))).start()

    events = _run(agent, "tell sam I'm on my way, 10 minutes", on_send=tap_once)
    states = [p.state for p in _sends(events)]
    assert taps == [True]
    assert states == ["counting", "swapped", "counting", "going"]   # the countdown started again for the new person
    swapped = _sends(events)[1]
    assert swapped.who == "Maya" and "Sam" in swapped.who_alts
    assert _sends(events)[2].who == "Maya"
    assert desk.sent == [TEXT]                                     # once, after the second countdown
    assert "send it to Maya instead" in seen["text"] and "tapped on the send countdown" in seen["text"]
    assert "Do not send it to Sam" in seen["text"]


def test_a_chip_value_that_was_not_offered_is_refused(tmp_path: Path) -> None:
    agent = _agent(Brain([]), tmp_path, _desk())
    assert agent.send_choice("who", "Maya") is False               # no countdown open
    agent._send_open = {"who_alts": ("Maya",), "where_alts": ()}
    assert agent.send_choice("who", "Someone else") is False and agent.send_choice("where", "Maya") is False
    assert agent.send_choice("who", "Maya") is True


def test_a_draft_never_gets_the_countdown(tmp_path: Path) -> None:
    desk = _desk()
    brain = Brain([_response(_use("type_text", text=TEXT, expect_hwnd=CHAT),
                             _use("done", message="It's in the box, not sent.", about=[], states=[],
                                  achieved="Drafted your message to Sam"))])
    events = _run(_agent(brain, tmp_path, desk, countdown=5.0), "draft a message to sam saying omw")
    assert _sends(events) == [] and desk.sent == []


def test_after_the_user_answered_a_question_the_send_goes_without_a_countdown(tmp_path: Path) -> None:
    """Only the person named ("message Sam"): Yuki asks what to say; their answer is the go-ahead."""
    desk = _desk()
    brain = Brain([_response(_use("ask_user", question="What should I tell Sam?")), _check(),
                   _response(_send(), _done()), _response(_done(1))])
    events = _run(_agent(brain, tmp_path, desk, countdown=5.0), "message sam", answers=[TEXT])
    assert _sends(events) == [] and desk.sent == [TEXT]


def test_the_countdown_is_off_unless_the_caller_turns_it_on(tmp_path: Path) -> None:
    desk = _desk()
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    events = _run(_agent(brain, tmp_path, desk, countdown=0.0), "tell sam omw")   # the CLI, evals, tests
    assert _sends(events) == [] and desk.sent == [TEXT]


def test_the_desktop_runtime_turns_it_on_for_its_own_worker_only() -> None:
    from yuki.ui.runtime import SEND_COUNTDOWN_S

    assert SEND_COUNTDOWN_S == 3.0
    src = Path(__file__).resolve().parents[1].joinpath("yuki", "ui", "runtime.py").read_text(encoding="utf-8")
    assert "worker_agent.send_countdown_s = SEND_COUNTDOWN_S" in src


def test_a_spoken_send_says_one_short_line(tmp_path: Path) -> None:
    from yuki.log.events import Say

    desk = _desk()
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, countdown=0.1)
    events = list(agent.run("tell sam omw", voice=True))
    says = [e.text for e in events if isinstance(e, Say)]
    assert says == ["Sending to Sam"] and desk.sent == [TEXT]


# -- 3. the brain's own live line ----------------------------------------------------------------------------------


def test_the_situation_checks_plain_line_and_the_now_note_reach_the_island(tmp_path: Path) -> None:
    desk = _desk()
    mem = FakeMemoryClient(own=[{"text": TEXT, "at": NOW - 1800, "first_seen": NOW - 1800, "thread": 3}])
    note = "Nothing like it in the chat, so sending it now"
    brain = Brain([_check(), _response(_send(now=note), _done()), _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, countdown=0.0, memory=MemoryAccess(open_client=lambda: mem))
    events = list(agent.run("tell sam omw"))
    nows = [e.text for e in events if isinstance(e, Partial) and e.what == "now"]
    stamp = time.strftime("%I:%M %p", time.localtime(NOW - 1800)).lstrip("0").replace(":00 ", " ")
    assert nows[0] == f"Checked Discord first: you sent something close to this at {stamp}"
    assert nows[1] == note
    assert TEXT not in nows[0]                                     # what was looked at, never the content
    call = next(i for i, e in enumerate(events) if isinstance(e, ToolCall) and e.name == "type_text")
    assert isinstance(events[call + 1], Partial) and events[call + 1].text == note   # right after its step


def test_the_situation_line_without_a_close_message_names_the_last_one(tmp_path: Path) -> None:
    from yuki.agent.situation import Situation

    sit = Situation(action="x", hwnd=CHAT, person={"found": True, "name": "Sam", "routes": [
        {"label": "Discord DM", "last_mine": NOW - 86400 - 60}]}, now=NOW)
    assert sit.shown("Discord").startswith("Checked Discord first: your last message to Sam was yesterday at ")
    assert Situation(action="x").shown() == "Checked memory first"


def test_the_now_note_is_never_compared_or_judged() -> None:
    from yuki.agent.judge import call_text
    from yuki.agent.loopguard import signature

    a = {"text": TEXT, "press_enter": True, "now": "Checking the chat first"}
    b = {"text": TEXT, "press_enter": True, "now": "Sending it now"}
    assert signature(a) == signature(b)
    assert "Checking the chat first" not in call_text("type_text", a)


def test_the_now_field_is_optional_and_on_the_step_tools() -> None:
    from yuki.agent.tools import NOW_FIELD, NOW_TOOLS, TOOLS_BY_NAME

    for name in ("type_text", "click", "check_situation", "look_at_window"):
        schema = TOOLS_BY_NAME[name]["input_schema"]
        assert NOW_FIELD in schema["properties"] and NOW_FIELD not in schema.get("required", [])
    assert "done" not in NOW_TOOLS and "ask_user" not in NOW_TOOLS


def test_plain_thought_keeps_plain_lines_and_drops_code() -> None:
    from yuki.ui.step_words import plain_thought

    assert plain_thought("  checking the chat first so it isn't sent twice. ") == \
        "Checking the chat first so it isn't sent twice"
    assert plain_thought("Last message at 6:08 PM") == "Last message at 6:08 PM"
    assert plain_thought("Get-Process slack | Stop-Process") == ""
    assert plain_thought("open C:\\Users\\x\\a.txt") == ""
    assert plain_thought(None) == ""


# -- review fixes (report 49 "Review fixes") -------------------------------------------------------------------------


def _frame_with_maya(monkeypatch: pytest.MonkeyPatch, routes: list[dict[str, Any]] | None = None) -> None:
    from yuki.agent import frame as task_frame
    from yuki.agent.frame import Candidate, TaskFrame

    real = task_frame.build

    def build(*a: Any, **k: Any) -> TaskFrame:          # the resolver had Sam first, Maya close behind (invented)
        frame = real(*a, **k)
        frame.persons = [{"id": 1, "name": "Sam", "routes": routes or [
            {"label": "Discord DM @sam_99", "app": "Discord.exe"}, {"label": "WhatsApp", "app": "WhatsApp.exe"}]}]
        frame.people = [Candidate("person", "Sam", "said_user", "you said", 5.0, 1.0),
                        Candidate("person", "Maya", "recent", "a chat 4 min ago", 240.0, 0.8)]
        return frame

    monkeypatch.setattr(task_frame, "build", build)


def _tap_once(kind: str, value: str, taps: list[bool]) -> Any:
    def tap(a: Any, _ev: SendPending) -> None:
        if not taps:
            taps.append(False)
            threading.Timer(0.05, lambda: taps.__setitem__(0, a.send_choice(kind, value))).start()

    return tap


def test_the_card_names_the_chat_the_window_really_shows(tmp_path: Path) -> None:
    """F1: the brain believes it is Sam's chat, the window in front is Maya's: the card shows the window's own title."""
    desk = ChatPlayerDesk(friend="Maya", handle="maya_k", history=[("Maya", NOW - 3000, "hey")])
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    events = _run(_agent(brain, tmp_path, desk, countdown=0.1), "tell sam omw")
    first = _sends(events)[0]
    assert first.who == "Sam" and first.window == "@maya_k - Discord"   # the wrong chat is on the card, as is


def test_a_who_swap_never_names_the_old_persons_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: "send it to Maya instead, on Discord", never "on Discord DM @sam_99" (Sam's own chat)."""
    _frame_with_maya(monkeypatch)
    seen: dict[str, str] = {}

    def after(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_done(3))

    brain = Brain([_check(), _response(_send()), after, _response(_done(4))])
    taps: list[bool] = []
    _run(_agent(brain, tmp_path, _desk(), countdown=0.5), "tell sam omw", on_send=_tap_once("who", "Maya", taps))
    lines = [ln for ln in seen["text"].splitlines() if "Maya" in ln]
    assert taps == [True] and any("send it to Maya instead, on Discord." in ln for ln in lines)
    assert "@sam_99" not in "\n".join(lines) and "Do not send it to Sam." in seen["text"]


def test_a_where_swap_names_the_new_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: after a swap to WhatsApp the switching card says WhatsApp, not the old app."""
    _frame_with_maya(monkeypatch)
    brain = Brain([_check(), _response(_send()), _response(_done(3)), _response(_done(4))])
    taps: list[bool] = []
    events = _run(_agent(brain, tmp_path, _desk(), countdown=0.5), "tell sam omw",
                  on_send=_tap_once("where", "WhatsApp", taps))
    swapped = [p for p in _sends(events) if p.state == "swapped"]
    assert taps == [True] and swapped and swapped[0].where == "WhatsApp" and swapped[0].app == "WhatsApp"
    assert swapped[0].window == ""                                 # not the window it was going into any more


def test_a_swap_after_typing_drops_the_old_box_and_says_the_text_is_still_there(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F3: typed "omw", Enter counts down, swap to Maya: the old box's text is no longer the next send's text, a click
    to the new chat is no send (no countdown for it), and the brain hears the text is still in the old box."""
    _frame_with_maya(monkeypatch, routes=[{"label": "Discord DM @sam_99", "app": "Discord.exe"}])
    desk = _desk()
    seen: dict[str, str] = {}

    def after_swap(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _check(3, about="Maya")

    brain = Brain([_check(),
                   _response(_use("type_text", 1, text="omw", expect_hwnd=CHAT)),
                   _response(_use("type_text", 2, text="", press_enter=True, expect_hwnd=CHAT)),
                   after_swap,
                   _response(_use("click", 5, x=10, y=10, expect_hwnd=CHAT)),
                   _response(_use("type_text", 4, text="omw", press_enter=True, expect_hwnd=CHAT), _done()),
                   _response(_done(5))])
    taps: list[bool] = []
    events = _run(_agent(brain, tmp_path, desk, countdown=0.4), "tell sam omw", on_send=_tap_once("who", "Maya", taps))
    got = [(p.state, p.who, p.text, p.typed) for p in _sends(events)]
    assert got[0] == ("counting", "Sam", "omw", True) and got[1][0] == "swapped"
    assert got[2:] == [("counting", "Maya", "omw", False), ("going", "Maya", "omw", False)]   # never "omwomw"
    assert "still in the message box of the chat you were in, not sent" in seen["text"]


def test_an_answer_about_who_or_where_is_no_go_ahead_on_the_text(tmp_path: Path) -> None:
    """F7: "Discord or WhatsApp?" -> "Discord" picks a place; the plain send still gets its countdown."""
    desk = _desk()
    brain = Brain([_response(_use("ask_user", question="Sam on Discord or Sam on WhatsApp?")), _check(),
                   _response(_send(), _done()), _response(_done(1))])
    events = _run(_agent(brain, tmp_path, desk, countdown=0.1), "send omw, 10 min to sam", answers=["Discord"])
    assert [p.state for p in _sends(events)] == ["counting", "going"] and desk.sent == [TEXT]


def test_a_stop_in_the_countdown_ends_the_run_saying_nothing_was_sent(tmp_path: Path) -> None:
    """The run's cancel line (what the screen and the voice get) says plainly that the message was not sent."""
    brain = Brain([_check(), _response(_send(), _done()), _response(_done(1))])
    agent = _agent(brain, tmp_path, _desk(), countdown=5.0)
    events = _run(agent, "tell sam omw", on_send=lambda a, _ev: threading.Timer(0.05, a.cancel).start())
    assert isinstance(events[-1], ErrorEvent) and events[-1].text.startswith("Cancelled by the user.")
    assert "The message was not sent." in events[-1].text


def test_the_now_note_streams_out_before_its_step_starts() -> None:
    """F4: the brain's note the moment its string is complete in the streaming call, before the ToolCall: the island's
    thinking line has it before the first step. The key, never the same word as another field's value."""
    from yuki.agent.stream_partial import stream_partial, string_value

    state: dict[str, Any] = {}
    start = SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", name="click"))
    assert stream_partial(start, state) is None
    parts = []
    for chunk in ('{"x": 4, "y": 9, "now": "Opening Sam', "'s chat to check it first\"", ', "expect_hwnd": 3}'):
        p = stream_partial(SimpleNamespace(type="input_json", partial_json=chunk, snapshot={}), state)
        if p is not None:
            parts.append((p.what, p.text))
    assert parts == [("now", "Opening Sam's chat to check it first")]
    assert string_value('{"text": "now", "what": "x"}', "now") is None
    done = SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", name="done"))
    stream_partial(done, state)
    assert stream_partial(SimpleNamespace(type="input_json", partial_json='{"now": "x"', snapshot={}), state) is None


def test_plain_thought_drops_window_and_element_numbers() -> None:
    """F8: ids never show; times, counts and "4pm" do."""
    from yuki.agent.tools import NOW_SCHEMA
    from yuki.ui.step_words import plain_thought

    assert plain_thought("Reading hwnd 655432 to find Sam") == ""
    assert plain_thought("Looking at element e12 in the tree") == ""
    assert plain_thought("Your last message was at 6:08 PM, 3 messages ago") == \
        "Your last message was at 6:08 PM, 3 messages ago"
    assert plain_thought("Meeting at 4pm on the 2nd") == "Meeting at 4pm on the 2nd"
    assert "ids" in NOW_SCHEMA["description"]


def test_the_talker_knows_a_send_is_counting_down_so_stop_is_for_it() -> None:
    """Item 1 "saying stop cancels": while the countdown runs, the talker's thinker line says the job is about to send
    and that their stop is for that send (cancel_tell_yuki); the line goes back to normal once it went or stopped."""
    import asyncio

    from PySide6.QtCore import QCoreApplication, QObject, Signal

    from yuki.voice.bridge import VoiceBridge

    app = QCoreApplication.instance() or QCoreApplication([])

    class Runtime(QObject):
        started = Signal(str, int, str)
        tool_called = Signal(str, int, str, dict)
        send_pending = Signal(str, int, dict)

        def submit(self, text: str, **kw: Any) -> tuple[str, int]:
            return "worker", 7

    class Params:
        def __init__(self) -> None:
            self.arguments = {"screen": False, "say_first": "On it.", "request": "Tell Sam omw", "intent": "new_task",
                              "acts": True}
            self.tool_call_id, self.function_name = "call-1", "tell_yuki"
            self.context = SimpleNamespace(get_messages=lambda: [{"role": "user", "content": "tell sam omw"}])
            self.llm = SimpleNamespace(push_frame=None)

        async def result_callback(self, result: dict, properties: Any = None) -> None:
            pass

    async def settle() -> None:
        for _ in range(8):
            app.processEvents()
            await asyncio.sleep(0)

    async def main() -> None:
        rt = Runtime()
        bridge = VoiceBridge(rt, foreground=lambda: 42)
        bridge.attach(asyncio.get_running_loop())
        task = asyncio.create_task(bridge.tell_yuki(Params()))
        await settle()
        rt.started.emit("worker", 7, "x")
        rt.tool_called.emit("worker", 7, "type_text", {"text": TEXT, "press_enter": True})
        await settle()
        rt.send_pending.emit("worker", 7, {"state": "counting", "who": "Sam"})
        await settle()
        line = bridge.work_line() or ""
        assert "about to send it to Sam" in line and "cancel_tell_yuki" in line
        rt.send_pending.emit("worker", 7, {"state": "going", "who": "Sam"})
        await settle()
        assert "about to send" not in (bridge.work_line() or "")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
