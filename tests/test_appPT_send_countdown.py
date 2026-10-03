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
