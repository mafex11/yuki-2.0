"""Revamp D, part 1: the brain's safety and speed (the design notes).

One piece per test: the authorization judge (gating by tool metadata, the stored policy, the deny limits, what it sees),
the finish gate (three verdicts, the learned expected-unverifiable control, the send confirmation), the consult (its
triggers, and that the actor's cached profile never changes), the output diet (the voice still reads a verified
finish line), the request pack, the merged memory tool, the reader lane, quieter hands (UIA patterns first, yielding to
the user, leases) and one Yuki in the prompt.

Fake model clients, stub desks and temp files only: no network, no desktop, no real memory, nothing sent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from yuki.agent.loop import JUDGE_LABEL, Agent
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import AskUser, ErrorEvent, Final, Say, SessionLogger, ToolCall
from yuki.voice.offline import StubDesktop, StubResult

CHROME = 0x1001


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _usage(inp: int = 10, out: int = 5, read: int = 0, write: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


def _response(*blocks: SimpleNamespace, stop: str = "tool_use", out: int = 5) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=_usage(out=out), id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class _JudgeStream:
    """A streamed forced-tool verdict, its JSON in a few chunks (the shape Bedrock streams)."""

    def __init__(self, verdict: dict[str, Any], tool: str) -> None:
        self.verdict, self.tool = verdict, tool

    def __enter__(self) -> "_JudgeStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_usage(inp=500, read=4300)))
        yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use"))
        text = json.dumps(self.verdict)
        for i in range(0, len(text), 9):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 9]))
        yield SimpleNamespace(type="message_stop")

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[_block("tool_use", name=self.tool, input=self.verdict)],
                               usage=_usage(inp=500, out=30, read=4300))


class Client:
    """Routes the brain's calls (scripted), the judge's streamed calls (a function of the request) and the consult's
    calls (scripted)."""

    def __init__(self, brain: list[SimpleNamespace], *, judge: Callable[[dict], dict] | None = None,
                 advice: list[dict] | None = None) -> None:
        self.brain = list(brain)
        self.brain_calls: list[dict] = []
        self.judge_calls: list[dict] = []
        self.consult_calls: list[dict] = []
        self.judge = judge or (lambda params: {"decision": "allow", "sends": False, "scope": "call", "why": "ok"})
        self.advice = list(advice or [])
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **params: Any) -> SimpleNamespace:
        if any(t.get("name") == "advise" for t in params.get("tools") or []):
            self.consult_calls.append(params)
            data = self.advice.pop(0) if self.advice else {"verdict": "proceed", "advice": "Looks right."}
            return _response(_block("tool_use", name="advise", input=data), out=60)
        self.brain_calls.append(params)
        return self.brain.pop(0)

    def stream(self, **params: Any) -> _JudgeStream:
        self.judge_calls.append(params)
        tool = params["tool_choice"]["name"]
        verdict = dict(self.judge(params))
        if tool == "review":
            verdict = {"asked": "x", "action": "y", **verdict}
        return _JudgeStream(verdict, tool)


def _memory_none() -> MemoryAccess:
    return MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))


def _settings(tmp: Path, **kw: Any) -> Settings:
    return Settings(**{"log_dir": tmp / "sessions", "stream": False, "requests_csv": tmp / "r.csv",
                       "judge": True, "consult": False, "send_confirm_s": 0.3, **kw})


def _agent(client: Client, tmp: Path, *, backend: Any = None, memory: Any = None, policy: Path | None = None,
           lane: str = "worker", **kw: Any) -> Agent:
    settings = _settings(tmp, **kw)
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=backend or StubDesktop(action_delay_s=0), prewarm=False, record_requests=False,
                  memory=memory or _memory_none(), lane=lane)
    if agent.authorizer is not None:
        from yuki.agent.judge import PolicyReader

        agent.authorizer.policy = PolicyReader(policy or tmp / "autonomy.json")
    return agent


def _texts(content: Any) -> str:
    out = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            if block.get("type") == "text":
                out.append(block.get("text") or "")
            elif block.get("type") == "tool_result":
                out.append(_texts(block.get("content") or []))
        elif isinstance(block, str):
            out.append(block)
    return "\n".join(out)


def _run(agent: Agent, words: str, answers: list[str] | None = None, **kw: Any) -> list[Any]:
    events = []
    answers = list(answers or [])
    for ev in agent.run(words, **kw):
        events.append(ev)
        if isinstance(ev, AskUser):
            agent.answer(answers.pop(0) if answers else "no")
    return events


# ---------------------------------------------------------------------------
# S1. The authorization judge
# ---------------------------------------------------------------------------


def test_the_gate_is_tool_metadata_and_the_stored_policy_never_words(tmp_path: Path) -> None:
    from yuki.agent.judge import POLICY_DEFAULTS, gate_reasons
    from yuki.agent.tools import tool_traits

    levels = dict(POLICY_DEFAULTS)
    gated = lambda name, args, lv=levels: bool(gate_reasons(tool_traits(name, args), lv))  # noqa: E731
    # can destroy or reach someone, whatever the words: always judged
    assert gated("window_action", {"hwnd": 1, "action": "close"})
    assert gated("hotkey", {"keys": ["ctrl", "w"]}) and gated("press", {"key": "k"})
    assert gated("run_powershell", {"command": "Get-Date"})
    assert gated("tab", {"hwnd": 1, "tab": "a", "action": "close"})
    assert tool_traits("type_text", {"text": "hi", "press_enter": True}).reaches
    # the same tools' harmless operations are not, under "do it and tell me"
    assert not gated("window_action", {"hwnd": 1, "action": "minimize"})
    assert not gated("tab", {"hwnd": 1, "tab": "a", "action": "select"})
    assert not gated("launch_app", {"query": "spotify"}) and not gated("open_url", {"url": "https://x"})
    # "Click and type: do it and tell me" (the default) does not judge clicks and typing; "ask before doing" does
    assert not gated("click", {"element": 3}) and not gated("type_text", {"text": "hi"})
    strict = {**levels, "click_type": "ask"}
    assert gated("click", {"element": 3}, strict) and gated("type_text", {"text": "hi"}, strict)
    relaxed = {**levels, "click_type": "do_and_tell"}
    assert not gated("click", {"element": 3}, relaxed) and not gated("type_text", {"text": "hi"}, relaxed)
    assert gated("type_text", {"text": "hi", "press_enter": True}, relaxed)   # a send stays judged


def test_the_policy_is_read_from_its_file_inside_the_users_rules(tmp_path: Path) -> None:
    from yuki.agent.judge import POLICY_CEILINGS, POLICY_DEFAULTS, PolicyReader, policy_path
    from yuki.ui import policy as ui_policy

    # the same defaults and ceilings as the UI's own policy module (read here as a file, no UI import at run time)
    assert POLICY_DEFAULTS == ui_policy.DEFAULTS
    assert POLICY_CEILINGS == {k.key: k.ceiling for k in ui_policy.KINDS}
    path = tmp_path / "autonomy.json"
    reader = PolicyReader(path)
    assert reader.levels() == POLICY_DEFAULTS                       # no file: the defaults
    ui_policy.save({"click_type": "do_and_tell", "send_messages": "do_and_tell", "money": "do_and_tell"}, path)
    levels = reader.levels()
    assert levels["click_type"] == "do_and_tell"                    # the user's choice
    assert levels["send_messages"] == "ask" and levels["money"] == "suggest"   # never past a rule
    time.sleep(0.02)
    ui_policy.set_level("click_type", "ask", path)
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert reader.levels()["click_type"] == "ask"                   # re-read when the file changes
    os.environ["LOCALAPPDATA"], old = str(tmp_path), os.environ.get("LOCALAPPDATA")
    try:
        assert policy_path() == tmp_path / "Yuki" / "autonomy.json"
    finally:
        if old is not None:
            os.environ["LOCALAPPDATA"] = old


def test_the_judge_sees_only_the_users_words_and_the_call(tmp_path: Path) -> None:
    from yuki.agent.judge import POLICY_DEFAULTS, judge_request
    from yuki.agent.tools import tool_traits

    params = judge_request([{"how": "voice", "text": "open chrome"}, {"how": "voice", "text": "restore the pages"}],
                           "window_action", {"hwnd": CHROME, "action": "close", "say_now": "Closing Chrome.",
                                             "risk": {"reversible": True, "user_asked": True}},
                           target={"app": "chrome.exe", "title": "New Tab - Google Chrome"},
                           levels=dict(POLICY_DEFAULTS), traits=tool_traits("window_action", {"action": "close"}),
                           model="haiku")
    message = params["messages"][0]["content"]
    assert len(params["messages"]) == 1 and params["tool_choice"] == {"type": "tool", "name": "verdict"}
    assert "restore the pages" in message and "chrome.exe" in message and '"close"' in message
    assert "Closing Chrome" not in message and "reversible" not in message   # Yuki's notes about the call stay out
    assert params["system"][0]["cache_control"]["type"] == "ephemeral"     # its own cached prompt
    assert "examples" in params["system"][0]["text"].lower()


def test_the_unasked_close_is_held_and_the_brain_asks_instead(tmp_path: Path) -> None:
    desk = StubDesktop(action_delay_s=0)

    def judge(params: dict) -> dict:
        message = params["messages"][0]["content"]
        if '"close"' in message:
            return {"decision": "ask_user", "sends": False, "scope": "call", "why": "they asked to restore pages",
                    "question": "Close Chrome? You asked me to restore the pages."}
        return {"decision": "allow", "sends": False, "scope": "call", "why": "ok"}

    client = Client([
        _response(_block("tool_use", id="t1", name="window_action", input={"hwnd": CHROME, "action": "close"})),
        _response(_block("tool_use", id="t2", name="hotkey", input={"keys": ["ctrl", "shift", "t"],
                                                                     "expect_hwnd": CHROME}),
                  _block("tool_use", id="t3", name="done", input={"message": "Pages are back.", "about": []})),
        # (the stub key reports nothing, so that done is sent back once for a look)
        _response(_block("tool_use", id="t4", name="done", input={"message": "Pages are back.", "about": []})),
    ], judge=judge)
    agent = _agent(client, tmp_path, backend=desk)
    events = _run(agent, "open chrome and restore the pages")
    assert not any(c[0] == "window_action" for c in desk.calls)          # Chrome was never closed
    assert any(c[0] == "hotkey" for c in desk.calls)                      # the safer way ran
    held = _texts(client.brain_calls[1]["messages"][-1]["content"])
    assert JUDGE_LABEL in held and "Close Chrome?" in held and "not run" in held
    assert isinstance(events[-1], Final)
    review = [c for c in client.judge_calls if c["tool_choice"]["name"] == "review"]
    assert len(review) == 1                                               # the hold went through the second stage
    assert agent.last_summary["judge"]["denials"] == 1


def test_three_denials_in_a_row_stop_the_request(tmp_path: Path) -> None:
    desk = StubDesktop(action_delay_s=0)
    ask = {"decision": "ask_user", "sends": False, "scope": "call", "why": "not asked", "question": "Close it?"}
    brain = [_response(_block("tool_use", id=f"t{i}", name="window_action",
                              input={"hwnd": CHROME, "action": "close", "say_now": str(i)})) for i in range(5)]
    client = Client(brain, judge=lambda p: ask)
    agent = _agent(client, tmp_path, backend=desk)
    events = _run(agent, "what's on my screen?")
    assert isinstance(events[-1], ErrorEvent) and "hadn't asked" in events[-1].text
    assert len(client.brain_calls) == 3 and not desk.calls
    # the same call again is not judged again (memo); different calls were
    assert agent.last_summary["judge"]["denials"] == 3


def test_deny_counters_reset_on_an_allow_and_cap_at_twenty(tmp_path: Path) -> None:
    from yuki.agent.judge import MAX_TOTAL_DENIALS, Authorizer, PolicyReader

    answers: list[str] = []

    def verdict(_p: Any) -> dict[str, Any]:
        decision = answers.pop(0)   # V5: a question goes only with ask_user (an allow with one is read as doubt)
        return {"decision": decision, "question": "q?" if decision == "ask_user" else "", "sends": False,
                "scope": "call", "why": "w"}

    client = Client([], judge=verdict)
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(tmp_path / "none.json"))
    for i in range(2):
        answers += ["ask_user", "ask_user"]          # each held call goes through both stages
        auth.check("window_action", {"hwnd": 1, "action": "close"}, words=[], signature=f"c{i}")
    assert auth.consecutive == 2 and auth.stopped is None
    answers.append("allow")
    auth.check("window_action", {"hwnd": 1, "action": "close"}, words=[], signature="ok")
    assert auth.consecutive == 0 and auth.denials == 2
    auth.denials = MAX_TOTAL_DENIALS
    assert auth.stopped and "20" in auth.stopped


def test_the_second_stage_decides_by_its_own_three_answers() -> None:
    """The careful look answers toward / lasting / asked_effect; the decision follows by the fixed rule (a step that
    only moves around is allowed; a lasting effect needs their words to ask for it), so the model cannot ask about
    "is this the right step" once it said the step changes nothing."""
    from yuki.agent.judge import review_decision

    assert review_decision({"toward": True, "lasting": False}) == "allow"
    assert review_decision({"toward": True, "lasting": True, "asked_effect": True}) == "allow"
    assert review_decision({"toward": True, "lasting": True, "asked_effect": False}) == "ask_user"
    # vfix3 (report 53): a step with no lasting effect is never held as "not asked for" ("Search in Your Library" for
    # "play my playlist"); only settings that ask first for that kind keep "toward" as a condition for it
    assert review_decision({"toward": False, "lasting": False}) == "allow"
    assert review_decision({"toward": False, "lasting": False}, strict=True) == "ask_user"
    assert review_decision({"toward": True}) is None


def test_a_held_navigation_step_is_rescued_by_the_second_stage(tmp_path: Path) -> None:
    from yuki.agent.judge import Authorizer, PolicyReader

    def judge(params: dict) -> dict:
        if params["tool_choice"]["name"] == "verdict":
            return {"decision": "ask_user", "sends": False, "scope": "call", "why": "may not be the tab",
                    "question": "Switch tabs?"}
        return {"toward": True, "lasting": False, "asked_effect": False, "sends": False, "scope": "call"}

    client = Client([], judge=lambda p: judge(p))
    client.stream = lambda **params: (client.judge_calls.append(params),
                                      _JudgeStream(judge(params), params["tool_choice"]["name"]))[1]
    client.messages.stream = client.stream
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(tmp_path / "none.json"))
    v = auth.check("hotkey", {"keys": ["ctrl", "tab"]}, words=[{"how": "voice", "text": "go to the llm os tab"}],
                   signature="s")
    assert v.allowed and v.source == "review" and len(client.judge_calls) == 2 and auth.denials == 0


def test_suggest_only_kinds_are_never_run_and_need_no_model(tmp_path: Path) -> None:
    from yuki.ui import policy as ui_policy

    path = tmp_path / "autonomy.json"
    ui_policy.save({"files": "suggest"}, path)
    desk = StubDesktop(action_delay_s=0)
    client = Client([
        _response(_block("tool_use", id="t1", name="run_powershell",
                         input={"command": "Remove-Item x", "risk": {"reversible": False, "user_asked": True}})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "I'd delete x.", "about": []})),
    ])
    agent = _agent(client, tmp_path, backend=desk, policy=path)
    _run(agent, "delete x")
    assert not desk.calls and not client.judge_calls
    assert "only suggest" in _texts(client.brain_calls[1]["messages"][-1]["content"])


def test_a_judge_that_cannot_run_holds_the_call(tmp_path: Path) -> None:
    from yuki.agent.judge import Authorizer, PolicyReader

    class Broken:
        messages = SimpleNamespace(stream=lambda **p: (_ for _ in ()).throw(RuntimeError("no network")))

    auth = Authorizer(lambda: Broken(), _settings(tmp_path), policy=PolicyReader(tmp_path / "none.json"))
    v = auth.check("press", {"key": "enter"}, words=[{"how": "typed", "text": "send it"}], signature="s")
    assert v is not None and not v.allowed and v.source == "error" and "could not run" in v.why


def test_a_window_grant_covers_later_quiet_steps_but_never_a_send(tmp_path: Path) -> None:
    from yuki.agent.judge import Authorizer, PolicyReader

    client = Client([], judge=lambda p: {"decision": "allow", "sends": False, "scope": "window", "why": "steps"})
    from yuki.ui import policy as ui_policy

    strict = tmp_path / "strict.json"                                # "Click and type: ask before doing"
    ui_policy.save({"click_type": "ask"}, strict)
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(strict))
    auth.check("click", {"element": 1}, words=[], target={"hwnd": 7}, signature="a")
    deadline = time.monotonic() + 2
    while 7 not in auth.grants and time.monotonic() < deadline:   # the rest of an early allow lands on its thread
        time.sleep(0.01)
    calls = len(client.judge_calls)
    v = auth.check("type_text", {"text": "hey"}, words=[], target={"hwnd": 7}, signature="b")
    assert v.allowed and v.source == "grant" and len(client.judge_calls) == calls
    auth.check("type_text", {"text": "hey", "press_enter": True}, words=[], target={"hwnd": 7}, signature="c")
    assert len(client.judge_calls) == calls + 1                    # a send is judged every time


# ---------------------------------------------------------------------------
# S2. The finish gate and the send confirmation
# ---------------------------------------------------------------------------


def test_three_verdicts_from_the_report_and_what_was_learned() -> None:
    from yuki.agent import finish

    assert finish.classify(True, True, "the page is at the address") == finish.VERIFIED
    assert finish.classify(True, None, finish.EXPECTED_UNVERIFIABLE) == finish.EXPECTED
    assert finish.classify(True, None, finish.LEARNED_UNVERIFIABLE.format(n=3)) == finish.EXPECTED
    assert finish.classify(True, False, "nothing changed") == finish.CONTRADICTED
    assert finish.classify(True, None, "its report cannot show the effect") == finish.UNVERIFIED
    assert finish.classify(False, False, "it failed") == finish.FAILED


def test_a_control_is_learned_expected_unverifiable_from_confirmed_runs(tmp_path: Path) -> None:
    from yuki.memory import practice as P
    from yuki.memory.store import Store

    store = Store.open(tmp_path / "m.db")
    try:
        def run(i: int, verdict: Any, confirmed: bool) -> None:
            P.record_run(store, {"source": "agent", "session_id": "s", "request_id": str(i), "at": time.time(),
                                 "words": "message him", "outcome": "final", "steps": [
                                     {"tool": "type_text", "kind": "action", "app": "Discord.exe", "ok": True,
                                      "verdict": verdict, "why": "", "control": "Edit|Message @x",
                                      "confirmed": confirmed, "input": {"text": "hi"}}]})

        run(1, None, True)
        rec = P.control_record(store, app="Discord.exe", tool="type_text", control="Edit|Message @x")
        assert rec["unverifiable_confirmed"] == 1
        run(2, None, True)
        run(3, None, False)
        rec = P.control_record(store, app="discord", tool="type_text", control="edit|message @x")
        assert rec == {"steps": 3, "verified": 0, "unverifiable_confirmed": 2, "unverifiable": 3, "contradicted": 0}
        assert P.control_record(store, app="Discord.exe", tool="click", control="Edit|Message @x")["steps"] == 0
        raw = store._query("SELECT target_ciphertext, playbooks FROM runs WHERE step=1")
        assert all("Message @x" not in (r["playbooks"] or "") for r in raw)   # the control is a keyed digest
    finally:
        store.close()


def test_the_loop_reads_a_learned_control_as_expected(tmp_path: Path) -> None:
    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: (_ for _ in ()).throw(RuntimeError("x")))

        @property
        def installed(self) -> bool:
            return True

        def call_method(self, name: str, *args: Any, timeout_s: float, **kwargs: Any) -> Any:
            if name == "control_record":
                return {"unverifiable_confirmed": 3, "contradicted": 0}
            return None

        def context(self, **kw: Any) -> Any:
            raise RuntimeError("no memory context in this test")

    class Desk(StubDesktop):
        def press(self, key: str, **kwargs: Any) -> StubResult:
            return self._act("press", f"pressed {key}", key, **kwargs)

    agent = _agent(Client([
        _response(_block("tool_use", id="t1", name="press", input={"key": "k"}),
                  _block("tool_use", id="t2", name="done", input={"message": "Paused.", "about": []})),
    ]), tmp_path, backend=Desk(action_delay_s=0), memory=Memory(), judge=False)
    agent.dispatcher.element_info = lambda tool_input: {"role": "Button", "name": "Pause"}
    agent.dispatcher._latest_tree_hwnd = CHROME
    events = _run(agent, "pause the video")
    assert isinstance(events[-1], Final) and events[-1].text == "Paused."   # not held for a look


def test_send_confirmation_waits_for_memorys_capture() -> None:
    from yuki.agent.finish import confirm_send

    seen: list[list[dict]] = [[], [], [{"text": "@Hemanth when are you coming on?", "at": 1.0, "thread": 4}]]
    refreshed: list[int] = []
    clock = {"t": 0.0}
    result = confirm_send(lambda since: seen.pop(0) if seen else [], "when are you coming on?", since=0.0,
                          within_s=4.0, refresh=lambda: refreshed.append(1), now=lambda: clock["t"],
                          sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    assert result["confirmed"] and result["similarity"] == 1.0 and result["polls"] == 3 and refreshed
    clock["t"] = 0.0
    nothing = confirm_send(lambda since: [{"text": "something else entirely"}], "hi there", since=0.0, within_s=1.0,
                           now=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    assert not nothing["confirmed"] and nothing["waited_ms"] >= 1000


def _send_memory(captured: list[dict]) -> MemoryAccess:
    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: (_ for _ in ()).throw(RuntimeError("x")))

        @property
        def installed(self) -> bool:
            return True

        def call_method(self, name: str, *args: Any, timeout_s: float, **kwargs: Any) -> Any:
            if name == "sent_messages":
                return list(captured)
            return None

        def context(self, **kw: Any) -> Any:
            raise RuntimeError("no memory context in this test")

    return Memory()


def test_a_confirmed_send_is_verified_and_an_unseen_one_is_said_plainly(tmp_path: Path) -> None:
    sends = lambda p: {"decision": "allow", "sends": True, "scope": "call", "why": "dictated"}  # noqa: E731
    for captured, expect in (([{"text": "on my way"}], "Verified: memory's capture"),
                             ([], "not confirmed")):
        client = Client([
            _response(_block("tool_use", id="t0", name="check_situation",
                             input={"action": "send the message", "hwnd": CHROME})),  # vfix2 N0: look first
            _response(_block("tool_use", id="t1", name="type_text",
                             input={"text": "on my way", "press_enter": True, "expect_hwnd": CHROME})),
            _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []})),
        ], judge=sends)
        agent = _agent(client, tmp_path, memory=_send_memory(captured))
        _run(agent, "tell hemanth on my way")
        result = _texts(client.brain_calls[2]["messages"][-1]["content"])
        assert expect in result
        assert agent.last_summary["sends"][0]["confirmed"] is bool(captured)


# ---------------------------------------------------------------------------
# S3. Consult instead of escalate
# ---------------------------------------------------------------------------


def _nothing_changed_desk() -> StubDesktop:
    class Desk(StubDesktop):
        def hotkey(self, *keys: str, **kwargs: Any) -> StubResult:
            r = self._act("hotkey", f"pressed {'+'.join(keys)}", *keys, **kwargs)
            r.details = {"foreground_changed": False, "focus_changed": False, "foreground_title_changed": False}
            return r

    return Desk(action_delay_s=0)


def test_a_contradicted_action_gets_one_second_look_and_the_profile_never_changes(tmp_path: Path) -> None:
    client = Client([
        _response(_block("tool_use", id="t1", name="hotkey", input={"keys": ["win", "up"]})),
        _response(_block("tool_use", id="t2", name="window_action", input={"hwnd": CHROME, "action": "fullscreen"}),
                  _block("tool_use", id="t3", name="done", input={"message": "Full screen.", "about": [CHROME]})),
    ], advice=[{"verdict": "revise", "advice": "Win+Up maximizes; use window_action fullscreen."}])
    agent = _agent(client, tmp_path, backend=_nothing_changed_desk(), judge=False, consult=True)
    events = _run(agent, "make chrome full screen", effort="low", thinking=False, escalate=("medium", True))
    assert isinstance(events[-1], Final)
    assert len(client.consult_calls) == 1
    consult = client.consult_calls[0]
    assert consult["thinking"] == {"type": "adaptive"} and consult["output_config"] == {"effort": "medium"}
    summary = consult["messages"][0]["content"]
    assert "make chrome full screen" in summary and "contradicted" in summary and len(summary) < 17_000
    note = _texts(client.brain_calls[1]["messages"][-1]["content"])
    assert "second look" in note and "window_action fullscreen" in note
    # the actor's cached profile: the same effort and thinking on every call of the request (no cache rewrite)
    profiles = {(json.dumps(c.get("output_config")), json.dumps(c["thinking"])) for c in client.brain_calls}
    assert len(profiles) == 1 and agent.last_summary["escalated"] is None
    assert agent.last_summary["consults"][0]["signal"] == "contradicted"


def test_a_pending_send_gets_a_second_look_that_can_hold_it(tmp_path: Path) -> None:
    desk = StubDesktop(action_delay_s=0)
    client = Client([
        _response(_block("tool_use", id="t0", name="check_situation",
                         input={"action": "send the message", "hwnd": CHROME})),  # vfix2 N0: look first
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"text": "hey", "press_enter": True, "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t2", name="ask_user", input={"question": "Send Hemanth 'hey'?"})),
        _response(_block("tool_use", id="t3", name="done", input={"message": "Okay, not sent.", "about": []})),
    ], judge=lambda p: {"decision": "allow", "sends": True, "scope": "call", "why": "w"},
        advice=[{"verdict": "ask_user", "blocking": True,
                 "advice": "Their words name Hemanth but no message: ask what to send."}])
    agent = _agent(client, tmp_path, backend=desk, consult=True)
    _run(agent, "message hemanth", answers=["no"])
    assert not any(c[0] == "type_text" for c in desk.calls)
    held = _texts(client.brain_calls[2]["messages"][-1]["content"])
    assert "second look" in held and "was not run" in held
    assert [c["signal"] for c in agent.last_summary["consults"]] == ["send"]
    # a wording quibble is advice, never a hold: the send runs
    desk2 = StubDesktop(action_delay_s=0)
    client2 = Client([
        _response(_block("tool_use", id="t0", name="check_situation",
                         input={"action": "send the message", "hwnd": CHROME})),  # vfix2 N0: look first
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"text": "kab aa raha hai vc pe", "press_enter": True, "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []})),
        _response(_block("tool_use", id="t3", name="done", input={"message": "Sent.", "about": []})),
    ], judge=lambda p: {"decision": "allow", "sends": True, "scope": "call", "why": "w"},
        advice=[{"verdict": "revise", "blocking": False, "advice": "They said 'coming on', not 'vc'."}])
    _run(_agent(client2, tmp_path, backend=desk2, consult=True), "ask hemanth when he's coming on")
    assert any(c[0] == "type_text" for c in desk2.calls)


def test_consults_are_capped_per_request(tmp_path: Path) -> None:
    brain = [_response(_block("tool_use", id=f"t{i}", name="hotkey", input={"keys": ["f", str(i)]}))
             for i in range(4)]
    brain.append(_response(_block("tool_use", id="d", name="done", input={"message": "Could not.", "about": []})))
    client = Client(brain)
    agent = _agent(client, tmp_path, backend=_nothing_changed_desk(), judge=False, consult=True, consult_max=2)
    _run(agent, "do the thing")
    assert len(client.consult_calls) == 2


# ---------------------------------------------------------------------------
# S4. The output diet, compatible with voice v3.17
# ---------------------------------------------------------------------------


def test_the_schemas_got_lighter_where_tool_metadata_allows() -> None:
    from yuki.agent.tools import (FINISH_WORDS_FIELD, FINISHES_FIELD, STEP_FIELD, TOOLS_BY_NAME, tool_params,
                                  with_step_lines)

    click = TOOLS_BY_NAME["click"]["input_schema"]["properties"]
    assert click[FINISHES_FIELD]["type"] == "boolean" and click[FINISH_WORDS_FIELD]["type"] == "string"
    assert "risk" not in click and STEP_FIELD not in click                 # no say_now on a typed request
    voice = {t["name"]: t for t in with_step_lines(tool_params(cacheable=False, screenshot_policy="never"))}
    assert STEP_FIELD in voice["click"]["input_schema"]["properties"]
    assert STEP_FIELD not in voice["click"]["input_schema"]["required"]    # optional now
    typed = len(json.dumps(tool_params(cacheable=False, screenshot_policy="never")))
    # was 70,190; vfix2 (report 47): +4.1k for check_situation, set_slider, tasks, states; appPT: +2.9k for the
    # optional `now` note on 19 tools (the island's live reasoning line, the user's 3 Oct ask)
    assert typed < 39_000, typed


def test_a_finishing_call_ends_with_a_line_written_by_code_that_the_voice_reads_as_verified(tmp_path: Path) -> None:
    from yuki.agent.tools import FINISHES_FIELD

    client = Client([_response(_block("tool_use", id="t1", name="window_action",
                                      input={"hwnd": CHROME, "action": "minimize", FINISHES_FIELD: True}))])
    agent = _agent(client, tmp_path, judge=False)
    events = _run(agent, "minimize chrome")
    call = next(e for e in events if isinstance(e, ToolCall))
    final = events[-1]
    assert isinstance(final, Final) and final.text == "Chrome is minimized." and len(client.brain_calls) == 1
    # what voice v3.17's bridge does (yuki/voice/bridge.py _on_tool_called / _on_tool_finished / the done check):
    finishing = call.input.get(FINISHES_FIELD)
    job_finishing = finishing.strip() if isinstance(finishing, str) and finishing.strip() else None
    assert job_finishing is not None and final.text.strip() == job_finishing   # -> said by code, verified


def test_finish_words_and_an_old_string_finish_still_work(tmp_path: Path) -> None:
    from yuki.agent.tools import FINISH_WORDS_FIELD, FINISHES_FIELD

    for extra in ({FINISHES_FIELD: True, FINISH_WORDS_FIELD: "It's down."}, {FINISHES_FIELD: "It's down."}):
        client = Client([_response(_block("tool_use", id="t1", name="window_action",
                                          input={"hwnd": CHROME, "action": "minimize", **extra}))])
        events = _run(_agent(client, tmp_path, judge=False), "minimize chrome")
        assert events[-1].text == "It's down."


# ---------------------------------------------------------------------------
# S5. The request pack
# ---------------------------------------------------------------------------


def test_run_takes_a_pack_built_ahead_and_rebuilds_its_screen_half_when_stale(tmp_path: Path) -> None:
    class Desk(StubDesktop):
        overviews = 0

        def get_desktop_overview(self) -> dict:
            Desk.overviews += 1
            return super().get_desktop_overview()

    desk = Desk(action_delay_s=0)
    client = Client([_response(_block("tool_use", id="t", name="done", input={"message": "Hi.", "about": []}))])
    at_first_call: list[int] = []
    create = client.create
    client.messages.create = lambda **p: (at_first_call.append(Desk.overviews), create(**p))[1]
    agent = _agent(client, tmp_path, backend=desk, judge=False)
    pack = agent.build_pack("hi", origin_hwnd=CHROME, view_hwnd=CHROME)
    assert pack.overview is not None and pack.view is not None and pack.origin and pack.build_ms >= 0
    built = Desk.overviews
    _run(agent, "hi", pack=pack)
    first = _texts(client.brain_calls[0]["messages"][0]["content"])
    assert "Inbox (3)" in first and "read for you as the request came in" in first.lower()
    assert at_first_call[0] == built        # nothing was read again before the first model call: it came from the pack
    client.brain.append(_response(_block("tool_use", id="t", name="done", input={"message": "Hi.", "about": []})))
    old = agent.build_pack("hi again")
    old.built_at -= 3600
    _run(agent, "hi again", pack=old)
    assert Desk.overviews > built + 1       # stale: read again


# ---------------------------------------------------------------------------
# S6. The merged memory tool, the reader lane, one Yuki
# ---------------------------------------------------------------------------


def test_the_seven_memory_reads_are_one_tool_by_kind(tmp_path: Path) -> None:
    from yuki.agent.tools import MEMORY_READ_KINDS, TOOLS_BY_NAME

    schema = TOOLS_BY_NAME["memory"]["input_schema"]
    assert schema["properties"]["kind"]["enum"] == list(MEMORY_READ_KINDS) and schema["required"] == ["kind"]
    for old in MEMORY_READ_KINDS:
        assert old not in TOOLS_BY_NAME
    seen: list[tuple[str, dict]] = []

    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: (_ for _ in ()).throw(RuntimeError("x")))

        def _activity(self, tool_input: dict) -> Any:
            from yuki.agent.tools import ToolOutcome

            seen.append(("activity", tool_input))
            return ToolOutcome(name="activity", ok=True, summary="3 apps", payload={})

    memory = Memory()
    out = memory.run_tool("memory", {"kind": "activity", "since": "2026-10-03"})
    assert out.ok and out.name == "memory" and out.payload["kind"] == "activity"
    assert seen == [("activity", {"since": "2026-10-03"})]
    bad = memory.run_tool("memory", {"kind": "teleport"})
    assert not bad.ok and "kind must be one of" in bad.summary
    # the call-timeline format lives in the tool's description now, not the core prompt
    assert "**13:24-13:51**" in TOOLS_BY_NAME["memory"]["description"]


def test_the_reader_lane_reads_only_and_hands_over_what_needs_hands(tmp_path: Path) -> None:
    from yuki.agent.lanes import READER_TOOLS, route
    from yuki.agent.tools import ACTION_TOOL_NAMES

    client = Client([
        _response(_block("tool_use", id="t1", name="hand_over", input={"why": "it needs a click"})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Right after this one.", "about": []})),
    ])
    agent = _agent(client, tmp_path, judge=False, lane="front_desk")
    assert agent.reader and set(agent.tool_names) <= set(READER_TOOLS) | {"ask_user", "done", "note_to_self", "say"}
    events = _run(agent, "open spotify")
    shown = {t["name"] for t in client.brain_calls[0]["tools"]}
    assert "hand_over" in shown and not shown & set(ACTION_TOOL_NAMES) and "memory" in shown
    calls = [e.name for e in events if isinstance(e, ToolCall)]
    # what the runtime does (Lane._drive): a call outside the agent's own subset means the request needs hands
    assert "hand_over" in calls and "hand_over" not in agent.tool_names
    assert "reader" in client.brain_calls[0]["system"][1]["text"]
    assert route(needs_hands=True, worker_busy=False) == "worker"
    assert route(needs_hands=False, worker_busy=False) == "reader"
    assert route(needs_hands=None, worker_busy=True) == "reader"
    assert route(needs_hands=None, worker_busy=False) == "worker"
    assert route(needs_hands=False, worker_busy=True, reader_busy=True) == "worker"


def test_one_yuki_the_prompt_owns_every_card_and_blames_no_component() -> None:
    from yuki.agent.prompt import SYSTEM_PROMPT, VOICE_LAYER, system_blocks
    from yuki.agent.tools import TOOLS_BY_NAME

    core = SYSTEM_PROMPT
    for gone in ("Your coach", "the coach who sent it", "why the coach went quiet", "the coach lines",
                 "coach sometimes asks"):
        assert gone not in core
    assert "There is one Yuki, and it is you" in core
    assert "You said on a card at" in core and "older wording that names a coach" in core   # both bracket wordings
    assert "own it: say in a few words what you noticed and when" in core
    assert "no coach, assistant or module apart from you" in core
    # the procedure loop, in order
    loop = core.index("resolve, plan when the stakes call for it, act, verify, record")
    assert loop < core.index("Resolve what") < core.index("Plan when") < core.index("Act.") < core.index("Verify.") \
        < core.index("Record what")
    assert len(core) <= 13_000, len(core)                                   # was 31,761 characters
    # the spoken rules live in the voice layer, sent only with a spoken request
    assert "say_now" not in core and "say_now" in VOICE_LAYER
    assert VOICE_LAYER not in "".join(b["text"] for b in system_blocks())
    assert VOICE_LAYER in "".join(b["text"] for b in system_blocks(voice=True))
    # Yuki's own fixed strings never present a separate coach either
    from yuki.agent import memory as agent_memory

    for text in (*agent_memory.STANDING_SECTION_HEADINGS, *(t["description"] for t in TOOLS_BY_NAME.values())):
        assert "coach" not in text.lower(), text


def test_memorys_standing_context_speaks_of_your_cards_not_a_coach() -> None:
    from yuki.memory.api import STANDING_HEADINGS

    assert "coach" not in STANDING_HEADINGS["coach"].lower() and "your own cards" in STANDING_HEADINGS["coach"]


# ---------------------------------------------------------------------------
# S7. Quieter hands
# ---------------------------------------------------------------------------


def _read_window(dispatcher: Any, hwnd: int, elements: list[dict]) -> None:
    dispatcher._reads += 1
    dispatcher._latest_trees[hwnd] = (dispatcher._reads, {e["id"]: e for e in elements})
    dispatcher._latest_tree_hwnd = hwnd


def test_a_pattern_element_is_acted_on_through_uia_before_the_mouse(tmp_path: Path) -> None:
    from yuki.agent.tools import Dispatcher

    class Desk(StubDesktop):
        def __init__(self, answer: Any) -> None:
            super().__init__(action_delay_s=0)
            self.answer, self.patterned = answer, []

        def element_action(self, hwnd: int, element: dict, verb: str, **kw: Any) -> Any:
            self.patterned.append((hwnd, element["name"], verb, kw))
            return self.answer

    button = {"id": 4, "role": "Button", "name": "Restore", "center": (100, 200), "bounds": (90, 190, 110, 210),
              "patterns": ("invoke",)}
    field = {"id": 5, "role": "Edit", "name": "Search", "center": (300, 40), "bounds": (200, 30, 400, 50),
             "patterns": ("value",)}
    done = StubResult(summary="Invoke on Button \"Restore\" through UI Automation",
                      details={"via": "uia_pattern", "foreground_title_changed": True})
    desk = Desk(done)
    d = Dispatcher(desk)
    _read_window(d, CHROME, [button, field])
    out = d.dispatch("click", {"element": 4, "expect_hwnd": CHROME})
    assert out.ok and desk.patterned[0][2] == "click" and not any(c[0] == "click" for c in desk.calls)
    d.dispatch("type_text", {"element": 5, "expect_hwnd": CHROME, "text": "lofi", "clear": True})
    assert desk.patterned[1][2] == "set_value" and desk.patterned[1][3] == {"value": "lofi"}
    assert not any(c[0] == "type_text" for c in desk.calls)
    # typing that appends, or sends, stays on the keys
    d.dispatch("type_text", {"element": 5, "expect_hwnd": CHROME, "text": "x", "press_enter": True})
    assert any(c[0] == "type_text" for c in desk.calls)
    # no pattern to use (or it could not be found again): the mouse, as before
    fallback = Desk(None)
    d2 = Dispatcher(fallback)
    _read_window(d2, CHROME, [button])
    d2.dispatch("click", {"element": 4, "expect_hwnd": CHROME})
    assert any(c[0] == "click" for c in fallback.calls)
    # a right click or a double click is never a pattern
    d.dispatch("click", {"element": 4, "expect_hwnd": CHROME, "clicks": 2})
    assert any(c[0] == "click" for c in desk.calls)


def test_pattern_choice_follows_the_reads_own_pattern_list() -> None:
    from yuki.actions.patterns import usable

    assert usable({"patterns": ("invoke", "select")}, "click") == "invoke"
    assert usable({"patterns": ("select",)}, "click") == "select"
    assert usable({"patterns": ("toggle",)}, "click") == "toggle"
    assert usable({"patterns": ()}, "click") is None
    assert usable({"patterns": ("value",)}, "set_value") == "value"
    assert usable({"patterns": (), "is_scrollable": True}, "scroll") == "scroll"


class _Sig:
    """A stub input monitor (``yuki.actions.userinput``): the user's real input times by kind, listening since 0."""

    running = True

    def __init__(self, started_at: float = -60.0, **times: float) -> None:
        self.started_at = started_at
        self.times = dict(times)

    def last(self, kind: str) -> float:
        value = self.times.get(kind, 0.0)
        return float(value() if callable(value) else value)

    def latest(self, kinds=("keys", "click", "wheel", "mouse")):
        best = max(((self.last(k), k) for k in kinds), default=(0.0, None))
        return best if best[0] else (0.0, None)


#: Where the pretend clock of the gate tests starts (monotonic seconds; 0.0 means "never" to the input monitor).
T0 = 10.0


def _clocked_gate(**kw: Any) -> tuple[Any, dict[str, float]]:
    """An input gate on a pretend clock, with pretend key and cursor reads (V8: never the real keyboard in a test)."""
    from yuki.actions.leases import InputGate

    clock = {"t": T0}
    kw.setdefault("held", lambda: False)
    kw.setdefault("cursor", lambda: (500, 500))
    kw.setdefault("signal", None)
    gate = InputGate(sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"], **kw)
    return gate, clock


def _typing_until(gate: Any, clock: dict[str, float], until: float) -> None:
    """They type until ``until`` s into the wait (the input monitor sees a key on every look), then stop."""
    sig = _Sig(keys=lambda: min(clock["t"], T0 + until))
    gate.age, gate.signal = (lambda: max(0.0, clock["t"] - (T0 + until))), sig


def test_input_waits_while_they_type_and_says_nothing_under_three_seconds() -> None:
    # V8: they type for ~1 s (a key on every look), then stop: Yuki waits, silently
    gate, clock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.1)
    _typing_until(gate, clock, 1.0)
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["waited"] and result["ok"] and result["real"] and said == []
    assert 1700 <= result["ms"] <= 2100


def test_input_wait_says_one_line_only_past_three_seconds() -> None:
    gate, clock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.1)
    _typing_until(gate, clock, 4.0)   # typing on for 4 s
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["ok"] and len(said) == 1 and "wait a moment" in said[0]


def test_mouse_moves_alone_never_hold_yuki_back() -> None:
    # V8: GetLastInputInfo counts every mouse move; with the cursor moving and nothing held down it is no real input
    gate, clock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05,
                                cursor=lambda: (int(clock["t"] * 1000), 300))
    gate.age = lambda: 0.01   # input "just now" on every look: the mouse is moving
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["ok"] and result["real"] is False and result["ms"] <= 300 and said == []


def test_a_keystroke_just_before_the_wait_counts() -> None:
    # Review F3: the user typed 0.3 s before Yuki's action and nothing since. The input monitor saw the key: Yuki waits
    # until 0.8 s after it (main's quiet), never going ahead 0.25 s into a typing pause.
    gate, clock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05, signal=_Sig(keys=T0 - 0.3))
    gate.age = lambda: 0.3 + clock["t"] - T0   # the last input stays where it was
    said: list[str] = []
    result = gate.wait(notify=said.append)
    assert result["ok"] and result["real"] is True and result["kind"] == "keys" and said == []
    assert 450 <= result["ms"] <= 600            # 0.8 s after the keystroke, not 0.25 s into the wait
    # hotInput: the same recent input with no key, button or wheel measured (a twitching mouse sensor moves the input
    # clock 20-30 times a second, mostly without moving the cursor): no wait at all
    jitter, jclock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05, signal=_Sig())
    jitter.age = lambda: 0.03
    result = jitter.wait()
    assert result == {"waited": False, "ok": True, "ms": 0, "real": False}
    # with no input monitor listening, a key's kind cannot be known from the input clock: the keys and buttons are
    # watched for a moment (probe_s), and with none pressed Yuki goes ahead
    blind, bclock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=12.0, poll_s=0.05)
    blind.age = lambda: 0.3 + bclock["t"] - T0
    result = blind.wait()
    assert result["ok"] and result["real"] is False and 250 <= result["ms"] <= 300


def test_real_input_counts_a_press_since_the_last_look_but_not_one_before_the_first() -> None:
    # Review F4: a move and a quick click inside one tick read as the mouse moving; a key or button that went down since
    # the last look (Windows' "pressed since" bit, reported through held) counts, but the first look is a baseline
    from yuki.actions.leases import RealInput

    clock = {"t": 0.0}
    presses = iter([True, False, True])
    spots = iter([(0, 0), (40, 0), (90, 0)])   # the cursor moving on every look
    watch = RealInput(age=lambda: 0.0, held=lambda: next(presses), cursor=lambda: next(spots),
                      now=lambda: clock["t"])
    assert watch.poll() is None                 # an old press: baseline only
    clock["t"] = 0.1
    assert watch.poll() is None and watch.cursor_moved   # only the mouse moved
    clock["t"] = 0.2
    assert watch.poll() == 0.2                  # a click came with the move: real


def test_a_held_key_or_button_counts_as_real_input_and_a_long_one_stops_the_action() -> None:
    gate, _clock = _clocked_gate(recent_s=1.0, quiet_s=0.8, max_s=1.0, poll_s=0.1, held=lambda: True,
                                 cursor=lambda: None)
    gate.age = lambda: 0.1
    result = gate.wait()
    assert not result["ok"] and result["real"]
    idle, _ = _clocked_gate()
    idle.age = lambda: 30.0
    assert idle.wait() == {"waited": False, "ok": True, "ms": 0.0}


def test_yukis_own_input_is_never_taken_for_the_users() -> None:
    from yuki.actions.leases import user_input_age_s

    # the last input landed inside Yuki's own input action: the user has not touched anything since
    assert user_input_age_s(age=lambda: 0.4, span=lambda: (99.0, 99.8), now=lambda: 100.0) == float("inf")
    # the user's own input after Yuki's action
    assert user_input_age_s(age=lambda: 0.1, span=lambda: (90.0, 91.0), now=lambda: 100.0) == pytest.approx(0.1)


def test_the_dispatcher_yields_before_global_input_and_the_loop_says_it(tmp_path: Path) -> None:
    import queue

    from yuki.actions.leases import InputGate

    ages = iter([0.1, 0.2, 2.0])
    client = Client([_response(_block("tool_use", id="t1", name="press", input={"key": "enter"}),
                               _block("tool_use", id="t2", name="done", input={"message": "Done.", "about": []}))])
    desk = StubDesktop(action_delay_s=0)
    agent = _agent(client, tmp_path, backend=desk, judge=False)
    agent.dispatcher.notices = queue.SimpleQueue()
    # (pretend key and cursor reads: a test never reads the real keyboard; the line is said at once here)
    # a key held down on the first two looks (the first is only a baseline, report 76: the line needs a measured key)
    keys = iter([True, True])
    agent.dispatcher.input_gate = InputGate(recent_s=1.0, quiet_s=0.05, max_s=5.0, age=lambda: next(ages, 5.0),
                                            poll_s=0.01, notify_after_s=0.0, held=lambda: next(keys, False),
                                            cursor=lambda: (1, 1))
    events = _run(agent, "press enter")
    says = [e.text for e in events if isinstance(e, Say)]
    assert len(says) == 1 and "wait a moment" in says[0] and any(c[0] == "press" for c in desk.calls)
    # a media key acts system-wide and types nothing: no wait
    agent.dispatcher.input_gate = InputGate(age=lambda: 0.0, max_s=0.05, poll_s=0.01, held=lambda: True,
                                            cursor=lambda: (1, 1))
    out = agent.dispatcher.dispatch("press", {"key": "playpause"})
    assert out.ok
    blocked = agent.dispatcher.dispatch("press", {"key": "enter"})
    assert not blocked.ok and "Not sent" in blocked.summary


def test_leases_serialise_input_but_not_pattern_actions_on_other_windows() -> None:
    from yuki.actions.leases import LeaseBusy, input_lease, leased, window_lease

    held = threading.Event()
    release = threading.Event()

    def typer() -> None:
        with input_lease(11):
            held.set()
            release.wait(2)

    t = threading.Thread(target=typer)
    t.start()
    assert held.wait(2)
    with window_lease(22, timeout_s=0.2):      # a pattern action on another window does not wait
        pass
    with pytest.raises(LeaseBusy):
        with input_lease(timeout_s=0.1):       # a second global input does
            pass

    @leased
    def act(expect_hwnd: int | None = None) -> Any:
        return "ran"

    from yuki.actions import leases

    old = leases.INPUT_LEASE_S
    try:
        out = None
        box: dict = {}
        th = threading.Thread(target=lambda: box.setdefault("r", act(expect_hwnd=33)))
        th.start()
        release.set()
        th.join(5)
        out = box.get("r")
        assert out == "ran"
    finally:
        leases.INPUT_LEASE_S = old
        t.join(2)
