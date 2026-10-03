"""vfix (3 Oct, report 40 V1-V12): ending the conversation, stay_quiet final, the named app, the judge's doubt, repeats,
"act" never spoken, Roman script, the quiet input yield, web-page scrolls, keep scrolling, the home page check.

Offline only: fakes, stub desks and pretend clocks. No microphone, speaker, live desktop or real memory (ARCHITECTURE.md
rule 7). Every name, app pairing and message here is invented.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# V5: the judge fails closed on its own doubt (regressions from the two real cases, invented names)
# ---------------------------------------------------------------------------


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=500, output_tokens=30, cache_read_input_tokens=4300,
                           cache_creation_input_tokens=0)


class _Stream:
    def __init__(self, data: dict[str, Any], tool: str) -> None:
        self.data, self.tool = data, tool

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_usage()))
        text = json.dumps(self.data)
        for i in range(0, len(text), 7):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 7]))
        yield SimpleNamespace(type="message_stop")

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=self.tool, input=self.data)],
                               usage=_usage())


class _JudgeClient:
    """The quick verdict and the careful review, scripted (in the order the schemas write their fields)."""

    def __init__(self, verdict: dict[str, Any], review: dict[str, Any] | None = None) -> None:
        self.verdict, self.review = verdict, review
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(stream=self.stream, create=None)

    def stream(self, **params: Any) -> _Stream:
        self.calls.append(params)
        tool = params["tool_choice"]["name"]
        return _Stream(self.review if tool == "review" else self.verdict, tool)


def _authorizer(tmp_path: Any, client: _JudgeClient) -> Any:
    from yuki.agent.judge import Authorizer, PolicyReader

    settings = SimpleNamespace(judge_model="m", judge_timeout_s=5.0, estimate_cost=lambda m, u: 0.001)
    return Authorizer(lambda: client, settings, policy=PolicyReader(tmp_path / "none.json"))


_WORDS = [{"how": "heard", "text": "and then text Ravi on Chirp, ask when he's getting on the huddle"},
          {"how": "heard", "text": "because we need to sort something out"}]
_SEND = {"element": 120, "text": "huddle pe kab aayega?", "press_enter": True}
_TARGET = {"hwnd": 7001, "app": "Chirp.exe", "title": "Chirp", "element": 'Edit "Type a message to Ravi"'}


def test_v5_case_a_first_stage_asked_in_its_why_and_the_review_allowed_the_send(tmp_path: Any) -> None:
    """Like 3 Oct (first case): the quick check held the send, wrote its question into why (question empty), and the
    review allowed it. The decision and the reason disagreed and the message went out. Now: ask."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": "", "sends": True, "scope": "call",
         "why": "The request is clear, but the settings say to ask before sending. Should I send this to Ravi?"},
        {"asked": "ask Ravi on Chirp when he joins", "toward": True, "lasting": True, "asked_effect": True,
         "question": "", "sends": True, "scope": "window"})
    v = _authorizer(tmp_path, client).check("type_text", _SEND, words=_WORDS, target=_TARGET)
    assert v is not None and not v.allowed and v.decision == "ask_user"
    assert v.doubt == "stages_disagree" and v.source == "review"
    assert "Should I send this" in v.why and v.first_why == v.why   # the reason shown is the stage that asked
    assert len(client.calls) == 2


def test_v5_case_b_first_stage_wanted_the_exact_wording_and_the_review_allowed(tmp_path: Any) -> None:
    """Like 3 Oct (second case): "the exact wording matters for sends" with an allow. Now: ask, with a question."""
    client = _JudgeClient(
        {"decision": "ask_user", "question": "Send Ravi \"huddle pe kab aayega?\"?", "sends": True,
         "scope": "call", "why": "They said it in English, not in Hinglish. The exact wording matters for sends."},
        {"asked": "ask Ravi when he's getting on the huddle", "toward": True, "lasting": True,
         "asked_effect": True, "question": "", "sends": True, "scope": "call"})
    v = _authorizer(tmp_path, client).check("type_text", _SEND, words=_WORDS, target=_TARGET)
    assert not v.allowed and v.doubt == "stages_disagree"
    assert v.question.startswith("Send Ravi")


def test_v5_an_allow_that_comes_with_a_question_is_an_ask(tmp_path: Any) -> None:
    from yuki.agent.judge import decide, judge_request, POLICY_DEFAULTS
    from yuki.agent.tools import tool_traits

    client = _JudgeClient({"decision": "allow", "question": "Send it to Ravi?", "sends": True, "scope": "call",
                           "why": "ok"})
    params = judge_request(_WORDS, "type_text", _SEND, target=_TARGET, levels=dict(POLICY_DEFAULTS),
                           traits=tool_traits("type_text", _SEND), model="m")
    v = decide(client, params, need=("sends",))
    assert v.decision == "ask_user" and v.doubt == "question_with_allow" and v.question == "Send it to Ravi?"


def test_v5_a_clean_allow_still_returns_early_and_the_review_may_allow_a_step_that_lasts_nothing(tmp_path: Any) -> None:
    from yuki.agent.judge import decide, judge_request, POLICY_DEFAULTS
    from yuki.agent.tools import tool_traits

    client = _JudgeClient({"decision": "allow", "question": "", "sends": True, "scope": "window", "why": "ok"})
    params = judge_request(_WORDS, "type_text", _SEND, target=_TARGET, levels=dict(POLICY_DEFAULTS),
                           traits=tool_traits("type_text", _SEND), model="m")
    rest: list[Any] = []
    v = decide(client, params, need=("sends",), on_rest=rest.append)
    assert v.allowed and v.sends and v.question == ""
    # a navigation click the quick check held and the review called lasting-nothing: the review's allow stands
    click_client = _JudgeClient({"decision": "ask_user", "question": "Click it?", "sends": False, "scope": "call",
                                 "why": "unsure"},
                                {"asked": "open the list", "toward": True, "lasting": False, "asked_effect": False,
                                 "question": "", "sends": False, "scope": "call"})
    auth = _authorizer(tmp_path, click_client)
    v = auth.check("click", {"element": 4}, words=_WORDS,
                   target={"hwnd": 7002, "app": "Chirp.exe", "title": "Chirp", "role": "Button",
                           "element": 'Button "More"'})
    assert v.allowed and v.doubt is None and v.question == ""


def test_v5_a_review_with_a_question_is_its_own_doubt() -> None:
    from yuki.agent.judge import review_decision

    assert review_decision({"toward": True, "lasting": True, "asked_effect": True, "question": "Send it?"}) == \
        "ask_user"
    assert review_decision({"toward": True, "lasting": False, "question": ""}) == "allow"


def test_v5_the_schemas_put_the_question_next_to_the_decision() -> None:
    from yuki.agent.judge import REVIEW_TOOL, VERDICT_TOOL

    keys = list(VERDICT_TOOL["input_schema"]["properties"])
    assert keys[:2] == ["decision", "question"] and "question" in VERDICT_TOOL["input_schema"]["required"]
    review = list(REVIEW_TOOL["input_schema"]["properties"])
    assert review.index("question") < review.index("sends") and "question" in REVIEW_TOOL["input_schema"]["required"]


def test_v5_a_disagreed_allow_grants_no_window_and_memoizes_nothing(tmp_path: Any) -> None:
    client = _JudgeClient({"decision": "ask_user", "question": "", "sends": True, "scope": "call", "why": "unsure"},
                          {"asked": "x", "toward": True, "lasting": True, "asked_effect": True, "question": "",
                           "sends": True, "scope": "window"})
    auth = _authorizer(tmp_path, client)
    v = auth.check("type_text", _SEND, words=_WORDS, target=_TARGET)
    time.sleep(0.2)   # the review's rest is read on the judge's thread
    assert not v.allowed and 7001 not in auth.grants
    assert all(not m.allowed for m in auth.memo.values())


# ---------------------------------------------------------------------------
# V4: the app the user named wins, and the judge sees their heard words as text
# ---------------------------------------------------------------------------


def test_v4_the_judge_request_shows_the_named_app_and_a_worked_example_holds_another_app() -> None:
    from yuki.agent.judge import JUDGE_PROMPT, POLICY_DEFAULTS, judge_request
    from yuki.agent.tools import tool_traits

    params = judge_request(_WORDS, "type_text", _SEND, target={**_TARGET, "app": "Huddle.exe"},
                           levels=dict(POLICY_DEFAULTS), traits=tool_traits("type_text", _SEND), model="m",
                           named_app="Chirp")
    text = params["messages"][0]["content"]
    assert "named for this request" in text and "Chirp" in text and "never authorises" in text
    assert "You said Telegram" in JUDGE_PROMPT and "is not where it goes" in JUDGE_PROMPT
    plain = judge_request(_WORDS, "type_text", _SEND, target=_TARGET, levels=dict(POLICY_DEFAULTS),
                          traits=tool_traits("type_text", _SEND), model="m")
    assert "named for this request" not in plain["messages"][0]["content"]


def test_v4_heard_words_reach_the_judge_as_text_never_as_a_python_repr(tmp_path: Any) -> None:
    from yuki.agent.judge import words_text
    from yuki.ui.runtime import _words_tuple

    # the old voice shape, through the runtime: the text, not "{'how': ...}"
    assert _words_tuple([{"how": "voice", "text": "text Ravi on Chirp"}]) == ("text Ravi on Chirp",)
    from collections import deque

    from yuki.agent.loop import Agent

    agent = SimpleNamespace(_user_words=deque(maxlen=24), _requests=1, _request_voice=True)
    agent._note_words = lambda text, how, question=None: Agent._note_words(agent, text, how=how, question=question)
    Agent._note_request_words(agent, "Text Ravi on Chirp", "text ravi", [{"how": "voice", "text": "text Ravi on Chirp"},
                                                                         "ask when he's free"])
    words = list(agent._user_words)
    assert [w["how"] for w in words] == ["heard", "heard", "reading"]
    rendered = words_text(words)
    assert "{'how'" not in rendered and '"text Ravi on Chirp"' in rendered


class _FakeRuntime:
    """A runtime whose submit takes the voice's fields (probed by name, like the real one)."""

    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []
        self.named: list[tuple[int, str]] = []

    def submit(self, request: str, *, origin_hwnd: Any = None, source: str = "text", lane: Any = None,
               view_hwnd: Any = None, memory_query: Any = None, user_words: Any = None, pack: Any = None,
               needs_hands: Any = None, app_named: Any = None) -> tuple[str, int]:
        self.submits.append({"request": request, "user_words": user_words, "app_named": app_named})
        return "worker", len(self.submits)

    def name_app(self, request_id: int, app: str) -> bool:
        self.named.append((request_id, app))
        return True


def test_v4_the_bridge_passes_heard_words_as_strings_and_the_named_app_as_a_field() -> None:
    from yuki.voice.bridge import Job, VoiceBridge

    runtime = _FakeRuntime()
    bridge = VoiceBridge.__new__(VoiceBridge)
    bridge.runtime = runtime
    bridge._user_words_ok = None
    bridge._needs_hands_ok = None
    bridge._pack_ok = False
    bridge._lock = threading.Lock()
    bridge._unsubmitted, bridge._jobs = {}, {}
    bridge._log = lambda *a, **k: None
    bridge._changed = lambda: None
    bridge._job_event = lambda *a, **k: None
    bridge._take_pack = lambda: None
    job = Job(job_id=None, request="Text Ravi on Chirp", source="voice", token=1, events=None)
    bridge._submit(job, "Text Ravi on Chirp", None, user_words=["text Ravi on Chirp", "ask when"],
                   app_named="Chirp")
    sent = runtime.submits[-1]
    assert sent["user_words"] == ["text Ravi on Chirp", "ask when"] and sent["app_named"] == "Chirp"
    assert job.app_named == "Chirp"
    # an early-started job that is already submitted: the runtime gets the named app for its judge
    job.job_id = 1
    bridge._name_app(job, "Chirp")
    assert runtime.named == [(1, "Chirp")]


def test_v4_the_thinker_is_told_the_named_app_above_the_usual_route() -> None:
    from yuki.agent.frame import build, named_app_line
    from yuki.voice.mouth import TELL_YUKI, brain_request

    assert "app_named" in TELL_YUKI["required"]
    props = list(TELL_YUKI["properties"])
    assert props.index("app_named") < props.index("request")
    text = brain_request("Text Ravi on Chirp asking when he's getting on the huddle", ["text Ravi on Chirp"],
                         app_named="Chirp")
    assert "Where they said to do it, in their own words: Chirp" in text
    assert text.index("Chirp. Do it there") < text.index("[Asked by voice")
    frame = build({}, named_app="Chirp")
    lines = frame.text.splitlines()
    assert lines[1] == named_app_line("Chirp") and "usual route" in lines[1]   # first, right under the label
    assert build({}).text == ""


def test_v4_the_runtime_gives_the_named_app_to_the_agent_run() -> None:
    from yuki.ui.runtime import Job

    job = Job(1, "Text Ravi", None, "voice", None, None, None, ("text ravi",), None, "Chirp")
    assert job.app_named == "Chirp"


def test_v4_set_app_named_reaches_the_judge_call(tmp_path: Any) -> None:
    from yuki.agent.loop import Agent

    agent = SimpleNamespace(_request_app_named=None)
    Agent.set_app_named(agent, "  Chirp ")
    assert agent._request_app_named == "Chirp"
    Agent.set_app_named(agent, "")
    assert agent._request_app_named is None


# ---------------------------------------------------------------------------
# V1-V3: shut up / go away / bye end the conversation; stay_quiet is final
# ---------------------------------------------------------------------------


class _P:
    def __init__(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.function_name, self.arguments, self.tool_call_id = name, args or {}, f"c-{name}"
        self.results: list[tuple[dict[str, Any], Any]] = []
        self.spoken: list[str] = []

        async def push_frame(frame: Any) -> None:
            self.spoken.append(getattr(frame, "text", ""))

        self.llm = SimpleNamespace(push_frame=push_frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append((result, properties))


def _bridge() -> Any:
    from yuki.voice.bridge import VoiceBridge

    bridge = VoiceBridge.__new__(VoiceBridge)
    bridge._log = lambda *a, **k: None
    bridge.user_turn = lambda: True
    bridge.call_mode = lambda: False
    bridge.said_owed_at = None
    bridge.spoke_in_reply = lambda: False
    bridge.ledger = None
    bridge.held_at = None
    bridge.held_call = None
    bridge._held_refused = False
    bridge.named_turn = lambda: True          # the first words after the name
    bridge.in_exchange = lambda: True         # Yuki spoke a moment ago
    bridge.user_turns = lambda: 3
    bridge.call_evidence = lambda: None
    bridge.said_early = lambda call: False
    bridge.may_ack = lambda: True
    bridge.acked = lambda: None
    bridge.mark_spoken = None
    bridge.end_requested = None
    return bridge


def test_v2_a_stay_quiet_on_the_users_words_is_final_even_after_the_name_and_in_an_exchange() -> None:
    bridge = _bridge()
    p = _P("stay_quiet")
    asyncio.run(bridge.stay_quiet(p))
    result, props = p.results[-1]
    assert result == {"ok": True} and props.run_llm is False   # no "not_quiet", no second talker run


def test_v1_an_empty_goodbye_ends_at_once_and_a_goodbye_waits_for_its_words() -> None:
    bridge = _bridge()
    ends: list[bool] = []
    bridge.end_requested = lambda quick=False: ends.append(quick)
    asyncio.run(bridge.end_conversation(_P("end_conversation", {"say_first": ""})))
    asyncio.run(bridge.end_conversation(_P("end_conversation", {"say_first": "Later!"})))
    assert ends == [True, False]


def test_v1_v3_the_talker_prompt_ends_on_shut_up_go_away_and_bye_and_go_quiet_is_only_stop_listening() -> None:
    from yuki.voice.mouth import END_CONVERSATION, GO_QUIET, MOUTH_PROMPT, STAY_QUIET

    assert "shut up for a couple of hours" not in MOUTH_PROMPT and "shut up for" not in GO_QUIET["description"]
    assert '"ugh, shut up" or "go away" -> end_conversation(say_first="") alone' in MOUTH_PROMPT
    assert 'Bad: "shut up" -> go_quiet or stay_quiet' in MOUTH_PROMPT
    assert "a mistaken wake is a sign-off" in MOUTH_PROMPT
    assert "go back to sleep" in END_CONVERSATION["description"]
    assert '""' in END_CONVERSATION["properties"]["say_first"]["description"]
    assert "never this" in GO_QUIET["description"] and "final" in STAY_QUIET["description"]
    assert "With no call note, their words are for you unless they plainly are not" in MOUTH_PROMPT


# ---------------------------------------------------------------------------
# V9: never the same line twice; V6/marker: "act" is never spoken
# ---------------------------------------------------------------------------


def _speak(channel: Any, *chunks: str) -> str:
    out = "".join(channel.feed(c) for c in chunks)
    return out + channel.finish()


def test_v9_a_verbatim_repeat_of_the_last_line_is_never_said() -> None:
    from yuki.voice.mouth import ReplyChannel

    ch = ReplyChannel(repeat_of="Hey, how's it going?", heard="Hey, how's it going?")
    assert _speak(ch, "say\nHey, how", "'s it going?") == "" and ch.repeat
    assert ch.facts()["repeat_dropped"] is True


def test_v9_a_cut_line_said_again_from_its_start_is_a_repeat_of_what_they_heard() -> None:
    from yuki.voice.mouth import ReplyChannel

    line = "Nice. So you're building out the settings flow, it's looking really clean."
    ch = ReplyChannel(repeat_of=line, heard="Nice. So you're building out the")
    assert _speak(ch, "say\n" + line) == "" and ch.repeat


def test_v9_a_reply_that_goes_its_own_way_is_said_whole_and_short_lines_are_not_guarded() -> None:
    from yuki.voice.mouth import ReplyChannel

    ch = ReplyChannel(repeat_of="Hey, how's it going?", heard="Hey, how's it going?")
    assert _speak(ch, "say\nHey, how", " was your day?") == "Hey, how was your day?" and not ch.repeat
    short = ReplyChannel(repeat_of="Okay.", heard="Okay.")
    assert _speak(short, "say\nOkay.") == "Okay." and not short.repeat
    shorter = ReplyChannel(repeat_of="Hey, how's it going today?", heard="Hey, how's it going today?")
    assert _speak(shorter, "say\nHey") == "Hey" and not shorter.repeat   # a prefix shorter than what they heard


def test_v9_the_rerun_is_told_its_reply_was_a_repeat() -> None:
    from yuki.voice.mouth import REPEAT_NOTE, RERUN_NOTE, volatile_block

    text = volatile_block(rerun=True, state={"repeat": True}, channel="marker")["text"]
    assert REPEAT_NOTE in text and RERUN_NOTE not in text
    assert RERUN_NOTE in volatile_block(rerun=True)["text"]


def test_a_marker_line_inside_the_speech_is_never_spoken() -> None:
    from yuki.voice.mouth import ReplyChannel

    ch = ReplyChannel()
    assert _speak(ch, "say\nGot it. Let me pull it up.\n", "act\n") == "Got it. Let me pull it up.\n"
    assert ch.facts()["marker_in_speech"] == 1
    ch = ReplyChannel()
    assert _speak(ch, "say\nOkay.\nact") == "Okay.\n"   # the closing line with no line break
    ch = ReplyChannel()
    assert _speak(ch, "say\nActually it's open already.") == "Actually it's open already."
    ch = ReplyChannel()
    assert _speak(ch, "say\nSure.\nmore\nThe rest.") == "Sure.\n" and ch.held == "The rest."


# ---------------------------------------------------------------------------
# V10, V12, V6(a): the prompt
# ---------------------------------------------------------------------------


def test_v10_v12_v6_the_talker_writes_roman_script_short_and_only_what_was_reported() -> None:
    from yuki.voice.mouth import MOUTH_PROMPT, WORK_GUIDE

    assert "always in Roman letters" in MOUTH_PROMPT and "Never Devanagari" in MOUTH_PROMPT
    assert "with Hindi words in Devanagari" not in MOUTH_PROMPT
    assert "no praise of it, no question tacked on" in MOUTH_PROMPT and "unless asked" in MOUTH_PROMPT
    assert "never tell them it is doing a step it has not reported" in MOUTH_PROMPT
    assert "never a step it has not reached" in WORK_GUIDE


# ---------------------------------------------------------------------------
# V6(b): a web page's scroll is expected-unverifiable, never "did not scroll"
# ---------------------------------------------------------------------------


def test_v6_a_wheel_on_a_web_page_that_shows_no_movement_is_unverifiable_not_failed() -> None:
    from yuki.actions.scrolling import describe, wheel_at

    clock = {"t": 0.0}
    out = wheel_at(960, 520, send=lambda: None, marker=lambda: 500, cursor=lambda: (1, 1), move=lambda x, y: None,
                   clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s), page=True)
    assert out.moved is None and out.page and "could not be checked" in out.note
    ok, text = describe(out, direction="down", notches=5, x=960, y=520)
    assert ok and "on the web page" in text and "did not scroll" not in text
    # off a page the old measure stands: nothing moved is "did not scroll"
    out = wheel_at(960, 520, send=lambda: None, marker=lambda: 500, cursor=lambda: (1, 1), move=lambda x, y: None,
                   clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    assert out.moved is False and describe(out, direction="down", notches=5, x=960, y=520)[0] is False


def test_v6_the_finish_gate_calls_a_page_scroll_expected() -> None:
    from yuki.agent.finish import EXPECTED, PAGE_SCROLL_UNVERIFIABLE, classify
    from yuki.agent.loop import action_verdict
    from yuki.agent.tools import ToolOutcome

    outcome = ToolOutcome(name="scroll", ok=True, summary="sent", payload={"details": {
        "verified": None, "unverifiable": "page", "scrolls": [{"moved": None, "page": True}]}})
    verdict = action_verdict("scroll", outcome)
    assert verdict == (None, PAGE_SCROLL_UNVERIFIABLE) and classify(True, *verdict) == EXPECTED


class _PageDesk:
    """A stand-in desk whose page takes no UI Automation scrolling (a browser like the one of 3 Oct)."""

    def __init__(self) -> None:
        from yuki.voice.offline import BrowserDesk

        self.inner = BrowserDesk(action_delay_s=0)
        self.scrolls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def scroll(self, x: int, y: int, *, dy: int = 0, expect_hwnd: Any = None, **_: Any) -> Any:
        from yuki.voice.offline import StubResult

        self.scrolls += 1
        result = StubResult(ok=True, summary=f"sent {abs(dy)} wheel notch(es) on the web page; whether it moved could "
                                             f"not be checked")
        result.details = {"verified": None, "unverifiable": "page", "scrolls": [{"moved": None, "page": True}]}
        return result


def test_v6_the_scroll_hand_on_such_a_page_is_silent_and_never_handed_over() -> None:
    from yuki.voice.hands import Hands

    desk = _PageDesk()
    hands = Hands(desk)
    hands.snapshot()
    escalated: list[Any] = []
    hands.bridge = SimpleNamespace(escalate=lambda *a, **k: escalated.append(a), busy_tools=0, gate=None,
                                   _ack_if_silent=None, desk_job=lambda: None, may_ack=lambda: True)
    p = _P("scroll", {"window": desk.inner.CHROME, "direction": "down", "say_when_done": ""})
    asyncio.run(hands.handle(p))
    result, props = p.results[-1]
    assert "cannot be checked" in result["how_to_handle_it"] and props.run_llm is False
    assert not escalated and not hands.halted


# ---------------------------------------------------------------------------
# V7: keep scrolling until they say stop
# ---------------------------------------------------------------------------


@pytest.fixture()
def fast_scroll(monkeypatch: pytest.MonkeyPatch) -> Any:
    from yuki.actions import scrolling

    monkeypatch.setattr(scrolling, "TICK_S", 0.005)
    monkeypatch.setattr(scrolling, "PACES", {"slow": (0.04, 3), "normal": (0.02, 4), "fast": (0.01, 6)})
    yield scrolling
    scrolling.stop_continuous("ended", wait_s=1.0)
    scrolling.set_voice_hold(None)


def _wait_for(cond: Callable[[], bool], within_s: float = 6.0) -> bool:
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_v7_it_scrolls_in_steps_until_stopped_and_reports_where(fast_scroll: Any) -> None:
    s = fast_scroll
    steps: list[int] = []

    def step() -> Any:
        steps.append(1)
        return s.StepResult(ok=True, moved=True, percent=min(100.0, 5.0 * len(steps)))

    s.start_continuous(s.ContinuousScroll(step, where='Browser "Feed"', direction="down"))
    assert _wait_for(lambda: len(steps) >= 4)
    assert "Browser \"Feed\", down" in (s.continuous_state() or "")
    report = s.stop_continuous("voice")
    assert report["why"] == "voice" and report["steps"] >= 4 and report["percent"] is not None
    assert s.continuous_state() is None and "stopped as they asked" in s.stopped_words(report)


def test_v7_it_pauses_while_they_talk(fast_scroll: Any) -> None:
    s = fast_scroll
    talking = {"on": True}
    steps: list[int] = []
    s.set_voice_hold(lambda: talking["on"])

    def step() -> Any:
        steps.append(1)
        return s.StepResult(ok=True, moved=None)

    s.start_continuous(s.ContinuousScroll(step, where="Feed", hold=s.voice_hold))
    time.sleep(0.15)
    assert steps == []          # held the whole time they talked
    talking["on"] = False
    assert _wait_for(lambda: len(steps) >= 2)


def test_v7_it_stops_by_itself_at_the_end_when_they_take_over_and_after_failures(fast_scroll: Any) -> None:
    s = fast_scroll
    stops: list[dict[str, Any]] = []
    s.start_continuous(s.ContinuousScroll(lambda: s.StepResult(ok=True, moved=True, at_end=True), where="Feed",
                                          on_stop=stops.append))
    assert _wait_for(lambda: bool(stops)) and stops[-1]["why"] == "end"
    took = {"now": False}
    s.start_continuous(s.ContinuousScroll(lambda: s.StepResult(ok=True), where="Feed", on_stop=stops.append,
                                          took_over=lambda: took["now"]))
    time.sleep(0.05)
    took["now"] = True
    assert _wait_for(lambda: stops[-1]["why"] == "user_input")
    s.start_continuous(s.ContinuousScroll(lambda: s.StepResult(ok=False, report="refused"), where="Feed",
                                          on_stop=stops.append))
    assert _wait_for(lambda: stops[-1]["why"] == "failed")
    s.start_continuous(s.ContinuousScroll(lambda: s.StepResult(ok=True), where="Feed", on_stop=stops.append,
                                          max_steps=3))
    assert _wait_for(lambda: stops[-1]["why"] == "limit") and stops[-1]["steps"] == 3


def test_v7_a_new_scroll_replaces_the_running_one(fast_scroll: Any) -> None:
    s = fast_scroll
    first = s.ContinuousScroll(lambda: s.StepResult(ok=True), where="A")
    s.start_continuous(first)
    report = s.start_continuous(s.ContinuousScroll(lambda: s.StepResult(ok=True), where="B"))
    assert report is not None and report["why"] == "replaced" and not first.running
    assert "B" in (s.continuous_state() or "")


def test_v7_the_scroll_hand_keeps_going_on_a_shown_window_and_stops_by_voice(fast_scroll: Any) -> None:
    from yuki.voice.hands import HAND_TOOLS, Hands
    from yuki.voice.offline import BrowserDesk

    scroll = next(t for t in HAND_TOOLS if t["name"] == "scroll")
    assert scroll["properties"]["keep"]["enum"] == ["start", "stop"]   # one hand, no extra tool in the prefix
    desk = BrowserDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()
    p = _P("scroll", {"window": desk.CHROME, "direction": "down", "keep": "start", "say_when_done": ""})
    asyncio.run(hands.handle(p))
    result, props = p.results[-1]
    assert "scrolling down by itself" in result["done"] and props.run_llm is False
    assert _wait_for(lambda: desk.tabs[desk.selected]["scroll"] >= 24.0)   # it went on by itself
    stop = _P("scroll", {"window": desk.CHROME, "direction": "down", "keep": "stop", "say_when_done": ""})
    asyncio.run(hands.handle(stop))
    assert "stopped as they asked" in stop.results[-1][0]["done"]
    assert fast_scroll.continuous_state() is None
    # a window it was not shown is refused, never guessed
    bad = _P("scroll", {"window": 99, "direction": "down", "keep": "start", "say_when_done": ""})
    asyncio.run(hands.handle(bad))
    assert fast_scroll.continuous_state() is None


def test_v7_the_talker_sees_a_scrolling_line_while_it_runs() -> None:
    from yuki.voice.mouth import HANDS_PROMPT, volatile_block

    text = volatile_block(state={"scrolling": 'Browser "Feed", down, normal pace, 6 steps in 9 s'})["text"]
    assert "Scrolling now:" in text and 'keep "stop"' in text   # the stop rides with the state
    assert 'keep "start"' in HANDS_PROMPT and "Scrolling now" not in volatile_block()["text"]


def test_v7_the_thinkers_scroll_takes_until_stopped() -> None:
    from yuki.agent.tools import TOOL_SCHEMAS

    scroll = next(t for t in TOOL_SCHEMAS if t["name"] == "scroll")
    assert "until_stopped" in scroll["input_schema"]["properties"]


# ---------------------------------------------------------------------------
# V8 is in tests/test_d_brain.py (the input gate); V11: the home page check
# ---------------------------------------------------------------------------


def test_v11_another_page_of_the_site_is_not_its_home_page_when_it_was_already_showing() -> None:
    from yuki.actions.launch import _past_root, _same_page

    home = "https://www.videosite.example"
    assert _same_page(home, "https://www.videosite.example/watch?v=abc")          # loosely, any page of the site
    assert _past_root(home, "https://www.videosite.example/watch?v=abc")          # ... but past the root
    assert not _past_root(home, "https://www.videosite.example/")                 # the home page itself
    assert not _past_root("https://www.videosite.example/feed", "https://www.videosite.example/feed/x")
    assert not _past_root(home, "https://other.example/watch")


def test_v11_the_open_hands_ask_for_the_rest_of_the_request() -> None:
    from yuki.voice.hands import HAND_TOOLS

    for name in ("open_app", "open_url"):
        tool = next(t for t in HAND_TOOLS if t["name"] == name)
        assert "then" in tool["required"]
