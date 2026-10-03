"""vfix2 (3 Oct, report 47 N0-N10): think before acting (the situation check), the volume no-op, the slider hand, state
claims checked, toggles by state, the playlist, the addressee, pitfalls from troubled runs, misheard names, the task list
kept through interjections.

Offline only: fakes, stub desks, pretend clocks and tree SHAPES like the real Spotify and Discord ones. No microphone,
speaker, live desktop or real memory (ARCHITECTURE.md rule 7). Every name, app pairing and message here is invented.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_live_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brain's per-turn Now picture comes from memory's live service; here it is nothing (rule 7: these tests
    never read the real screen, even while Yuki runs)."""
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# N1: a volume change that cannot move is a no-op, said as one
# ---------------------------------------------------------------------------


def test_n1_app_volume_at_its_top_is_a_noop_that_names_the_other_two_volumes() -> None:
    from yuki.voice.offline import MixerStub

    mixer = MixerStub({"Spotify.exe": 100.0, "chrome.exe": 60.0}, playing={"Spotify.exe"}, system=35.0)
    up = mixer.app_volume("Spotify", change=10)
    assert up.ok and up.details["noop"] and up.details["verified"] and up.details["edge"] == "its top"
    assert up.details["before"] == up.details["after"] == 100.0 and mixer.levels["Spotify.exe"] == 100.0
    assert up.summary.startswith("No change:") and "nothing was changed" in up.summary
    assert "player volume" in up.summary and "system volume (now 35%)" in up.summary
    assert "turned up" not in up.summary.replace("never that it was turned up", "")
    # the same level asked again is a no-op too; a real move is not
    assert mixer.app_volume("chrome", level=60).details.get("noop")
    down = mixer.app_volume("Spotify", change=-20)
    assert down.ok and not down.details.get("noop") and mixer.levels["Spotify.exe"] == 80.0
    assert mixer.app_volume("Spotify", level=0).details["after"] == 0.0
    assert mixer.app_volume("Spotify", change=-5).details["edge"] == "its bottom"


def test_n1_the_finish_line_of_a_noop_says_nothing_changed_and_offers_the_others() -> None:
    from yuki.agent.finish import noop_line

    details = {"noop": True, "process": "Spotify.exe", "before": 100.0, "system_volume": 35.0,
               "asked": {"level": None, "change": 10}}
    line = noop_line("app_volume", details)
    assert line == ("Spotify is already at 100% in Windows' volume mixer, so nothing changed. Its own player volume "
                    "and the system volume, at 35%, are separate. Want me to raise one of those?")
    assert noop_line("app_volume", {"verified": True}) is None
    assert noop_line("set_slider", {"noop": True, "before_percent": 100.0}) == "It's already at 100%, so nothing changed."


def _hand(hands: Any, name: str, **args: Any) -> Any:
    spoken: list[str] = []
    results: list[Any] = []

    async def main() -> None:
        async def push_frame(frame: Any) -> None:
            spoken.append(frame.text)

        async def result_callback(result: Any, properties: Any = None) -> None:
            results.append(result)

        await hands.handle(SimpleNamespace(function_name=name, arguments=args, tool_call_id="c",
                                           llm=SimpleNamespace(push_frame=push_frame), result_callback=result_callback))

    asyncio.run(main())
    return SimpleNamespace(spoken=spoken, result=results[-1] if results else None)


def test_n1_the_voice_hand_never_says_turned_it_up_for_a_noop() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()
    assert desk.mixer.levels["chrome.exe"] == 100.0
    done = _hand(hands, "app_volume", app="chrome.exe", change=10, say_when_done="Turned it up.")
    assert done.spoken == []                                  # the done line is not said
    assert "no_change" in done.result and "already at 100%" in done.result["no_change"]
    assert "never that you turned it up" in done.result["how_to_say_it"]
    assert "passed_on" not in done.result                     # nothing to hand over: it is already there
    moved = _hand(hands, "app_volume", app="chrome.exe", change=-20, say_when_done="Turned it down.")
    assert moved.spoken == ["Turned it down."] and desk.mixer.levels["chrome.exe"] == 80.0


# ---------------------------------------------------------------------------
# N2: the slider hand (RangeValue, else its own keys, read back)
# ---------------------------------------------------------------------------


class FakeSlider:
    """A RangeValue slider: ``settable`` False makes SetValue do nothing (a web slider that ignores it)."""

    def __init__(self, value: float = 20.0, low: float = 0.0, high: float = 100.0, *, read_only: bool = False,
                 settable: bool = True, small: float = 2.0, keys_work: bool = True) -> None:
        self.value, self.low, self.high = value, low, high
        self.read_only, self.settable, self.small, self.keys_work = read_only, settable, small, keys_work
        self.focused = False
        self.sets: list[float] = []

    def read(self) -> tuple[float, float, float, bool, float]:
        return self.value, self.low, self.high, self.read_only, self.small

    def set(self, value: float) -> None:
        self.sets.append(value)
        if self.settable:
            self.value = value

    def focus(self) -> None:
        self.focused = True

    def press(self, key: str, times: int) -> None:
        assert self.focused, "keys go to the slider only after it has focus"
        if self.keys_work:
            step = self.small if key == "right" else -self.small
            self.value = max(self.low, min(self.high, self.value + step * times))


def test_n2_rangevalue_sets_the_level_and_reads_it_back() -> None:
    from yuki.actions.slider import drive

    s = FakeSlider(20.0)
    ok, summary, details = drive(s, to=70, press=s.press, sleep=lambda _s: None)
    assert ok and details["via"] == "RangeValue" and details["verified"] and s.value == 70.0
    assert "20% -> 70%" in summary and "the mouse was not used" in summary and not s.focused


def test_n2_a_slider_that_ignores_setvalue_moves_by_its_own_keys_with_read_back() -> None:
    from yuki.actions.slider import drive

    s = FakeSlider(20.0, settable=False, small=5.0)
    ok, summary, details = drive(s, by=30, press=s.press, sleep=lambda _s: None, settle_s=0.0)
    assert ok and details["via"] == "keys" and s.focused and s.value == 50.0 and details["presses"] == 6
    assert "its own arrow keys" in summary


def test_n2_already_at_the_top_is_a_noop_and_a_stuck_slider_says_so() -> None:
    from yuki.actions.slider import drive

    top = FakeSlider(100.0)
    ok, summary, details = drive(top, by=10, press=top.press, sleep=lambda _s: None)
    assert ok and details["noop"] and "already at 100%" in summary and top.sets == []
    stuck = FakeSlider(20.0, settable=False, keys_work=False)
    ok, summary, details = drive(stuck, to=80, press=stuck.press, sleep=lambda _s: None, settle_s=0.0)
    assert not ok and details.get("keys_stuck") and "Nothing was dragged or clicked" in summary


# ---------------------------------------------------------------------------
# The agent harness (a scripted brain, a judge function, a stub desk; nothing real)
# ---------------------------------------------------------------------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from yuki.agent.memory import MemoryAccess  # noqa: E402
from yuki.config import Settings  # noqa: E402
from yuki.log.events import AskUser, Final, SessionLogger, ToolResult  # noqa: E402
from yuki.voice.offline import ChatPlayerDesk  # noqa: E402

CHAT, PLAYER = ChatPlayerDesk.CHAT, ChatPlayerDesk.PLAYER


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _use(tool: str, i: int = 0, /, **inp: Any) -> SimpleNamespace:
    return _block("tool_use", id=f"t{tool}{i}{len(inp)}", name=tool, input=inp)


def _usage(inp: int = 10, out: int = 5, read: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=0)


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
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=_usage(inp=500, read=4300)))
        text = json.dumps(self.verdict)
        for i in range(0, len(text), 9):
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(partial_json=text[i:i + 9]))
        yield SimpleNamespace(type="message_stop")

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[_block("tool_use", name=self.tool, input=self.verdict)],
                               usage=_usage(inp=500, out=30, read=4300))


def _judge_sends(params: dict) -> dict:
    """Enter in a chat's message box sends; everything else in these replays is allowed and sends nothing."""
    sends = "press_enter" in json.dumps(params.get("messages") or [], default=str)
    return {"decision": "allow", "question": "", "sends": sends, "scope": "call", "why": "they asked for it"}


class Brain:
    """The brain's calls scripted (each a function of the request so far, or a fixed response)."""

    def __init__(self, script: list[Any], judge: Any = _judge_sends) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.judge = judge
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        step = self.script.pop(0)
        return step(params) if callable(step) else step

    def stream(self, **params: Any) -> _JudgeStream:
        tool = params["tool_choice"]["name"]
        verdict = dict(self.judge(params))
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
                    for c in b.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            out.append(str(c.get("text") or ""))
            elif isinstance(b, str):
                out.append(b)
    return "\n".join(out)


class FakeMemoryClient:
    """Memory's client as the situation check and the heard-name learner call it (invented people and messages)."""

    def __init__(self, own: list[dict[str, Any]] | None = None, person: dict[str, Any] | None = None) -> None:
        self.own = list(own or [])
        self.person = person
        self.aliases: list[tuple[str, str]] = []
        self.store = SimpleNamespace(add_alias=lambda heard, name, **kw: self.aliases.append((heard, name)) or 7)

    def contact(self, ref: Any, near: Any = None) -> dict | None:
        if self.person and str(ref).casefold() in {self.person["name"].casefold(), *self.person.get("aka", [])}:
            return {"found": True, **self.person}
        return {"found": False, "people": []}

    #: the desk whose sends memory's capture sees (its "me" messages), when one is given
    desk: Any = None

    def sent_messages(self, app: Any, since: float, *, limit: int = 12) -> list[dict]:
        seen = list(self.own)
        if self.desk is not None:
            known = {(m["text"], round(float(m["at"]))) for m in seen}
            seen += [{"text": text, "at": at, "first_seen": time.time(), "thread": 3, "status": None}
                     for who, at, text in self.desk.history if who == "me" and (text, round(at)) not in known]
        return [m for m in seen if float(m.get("first_seen") or m["at"]) >= since]

    def recall(self, query: str, **kw: Any) -> list[dict]:
        return []


def _memory(client: Any) -> MemoryAccess:
    access = MemoryAccess(open_client=lambda: client)
    return access


def _agent(brain: Brain, tmp: Path, desk: Any, memory: Any = None):
    from yuki.agent.judge import PolicyReader
    from yuki.agent.loop import Agent

    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv", judge=True,
                        consult=False, send_confirm_s=0.2)
    mem = memory or MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory")))
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=brain, backend=desk,
                  prewarm=False, record_requests=False, memory=mem, lane="worker")
    agent.authorizer.policy = PolicyReader(tmp / "autonomy.json")
    return agent


def _run(agent: Any, words: str, answers: list[str] | None = None, **kw: Any) -> list[Any]:
    events, answers = [], list(answers or [])
    for ev in agent.run(words, **kw):
        events.append(ev)
        if isinstance(ev, AskUser):
            agent.answer(answers.pop(0) if answers else "no")
    return events


# ---------------------------------------------------------------------------
# N0 / N5: think before acting - the situation check before a step that lasts
# ---------------------------------------------------------------------------

NOW = time.time()
EARLIER = "hey, I fell asleep on the call, sorry, my alarm did not go off"


def _stamp(at: float) -> str:
    return time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")


def _sam(minutes_ago: float | None = 60.0) -> tuple[ChatPlayerDesk, FakeMemoryClient]:
    history = [("Sam", NOW - 7200, "you joining the call?")]
    own: list[dict[str, Any]] = []
    if minutes_ago is not None:
        at = NOW - minutes_ago * 60
        history += [("me", at, EARLIER), ("Sam", at + 300, "lol no worries")]
        own = [{"text": EARLIER, "at": at, "first_seen": at, "thread": 3, "status": None}]
    desk = ChatPlayerDesk(friend="Sam", handle="sam_99", history=history)
    person = {"name": "Sam", "aka": ["sam", "sam_99"], "relation": "best friend", "routes": [
        {"label": "Discord DM @sam_99", "app": "Discord.exe", "last_mine": own[0]["at"] if own else NOW - 86400,
         "last_theirs": NOW - 3000, "samples": [{"text": m["text"], "at": m["at"]} for m in own]}]}
    mem = FakeMemoryClient(own=own, person=person)
    mem.desk = desk
    return desk, mem


def test_n0_situation_gather_reads_the_chat_and_memory_and_marks_the_similar_message() -> None:
    from yuki.agent.situation import gather

    desk, mem = _sam(60.0)
    sit = gather({"action": f"send Sam on Discord: {EARLIER}", "hwnd": CHAT, "about": "Sam"},
                 read_tree=desk.get_window_tree, process_of=lambda h: ("Discord.exe", "@sam_99 - Discord"),
                 memory_call=lambda method, *a, timeout_s: getattr(mem, method)(*a), now=NOW)
    text = sit.evidence_text()
    stamp = _stamp(NOW - 3600)
    assert f"You, {stamp}, {EARLIER}" in text                      # the chat's latest messages, newest last
    assert text.index("you joining the call?") < text.index("lol no worries")
    assert "the user's last message there" in text and "60 min ago" in text
    assert "(similar to what you are about to send: 1.00)" in text
    assert sit.summary().startswith("Checked first: read the window") and "one 1.00 similar" in sit.summary()
    assert EARLIER not in sit.summary()                           # the trace line names what was looked at, no content


def _held_duplicate_script(box: dict[str, Any]) -> list[Any]:
    def check(params: dict) -> SimpleNamespace:
        assert "nothing in this request has looked at the situation yet" in _texts(params)
        return _response(_use("check_situation", action=f"send Sam: {EARLIER}", hwnd=CHAT, about="Sam"))

    def ask(params: dict) -> SimpleNamespace:
        seen = _texts(params)
        stamp = _stamp(NOW - 3600)
        assert "similar to what you are about to send: 1.00" in seen and stamp in seen
        assert "what a good assistant would notice" in seen
        box["stamp"] = stamp
        return _response(_use("ask_user", question=f"You already told Sam the same thing at {stamp}. Send it again?"))

    return [_response(_use("type_text", text=EARLIER, press_enter=True, expect_hwnd=CHAT)), check, ask,
            _response(_use("done", message="Okay, I won't send it again.", about=[], states=[]))]


def test_n0_a_send_already_made_an_hour_ago_is_held_with_a_question_naming_the_time(tmp_path: Path) -> None:
    desk, mem = _sam(60.0)
    box: dict[str, Any] = {}
    agent = _agent(Brain(_held_duplicate_script(box)), tmp_path, desk, memory=_memory(mem))
    events = _run(agent, "tell sam I fell asleep on the call, my alarm did not go off", answers=["no, leave it"])
    asks = [e for e in events if isinstance(e, AskUser)]
    assert desk.sent == []                                          # nothing went out
    assert len(asks) == 1 and box["stamp"] in asks[0].question and "Send it again?" in asks[0].question
    held = [e for e in events if isinstance(e, ToolResult) and e.name == "type_text"]
    assert held and not held[0].ok
    trace = agent._trace
    assert [s["tool"] for s in trace][:2] == ["type_text", "check_situation"]
    assert trace[0]["why"] == "not run: no situation check before a step that lasts"
    assert trace[1]["result"].startswith("Checked first:")       # visible in the run trace


def test_n0_a_fresh_send_with_nothing_off_goes_through_without_an_extra_question(tmp_path: Path) -> None:
    desk, mem = _sam(None)                                          # nothing like it was ever sent
    text = "omw, 10 min"

    def send(params: dict) -> SimpleNamespace:
        assert "similar to what you are about to send" not in _texts(params)
        return _response(_use("type_text", text=text, press_enter=True, expect_hwnd=CHAT),
                         _use("done", message="Told Sam you're on your way.", about=[], states=[]))

    brain = Brain([_response(_use("check_situation", action=f"send Sam: {text}", hwnd=CHAT, about="Sam")), send])
    events = _run(_agent(brain, tmp_path, desk, memory=_memory(mem)), "tell sam I'm on my way, 10 minutes")
    assert desk.sent == [text] and not any(isinstance(e, AskUser) for e in events)
    assert any(isinstance(e, Final) for e in events)


def test_n0_a_check_sent_in_the_same_response_as_the_send_does_not_count(tmp_path: Path) -> None:
    desk, mem = _sam(None)
    text = "omw"
    brain = Brain([
        _response(_use("check_situation", action=f"send Sam: {text}", hwnd=CHAT, about="Sam"),
                  _use("type_text", text=text, press_enter=True, expect_hwnd=CHAT)),
        _response(_use("type_text", 1, text=text, press_enter=True, expect_hwnd=CHAT),
                  _use("done", message="Sent.", about=[], states=[])),
    ])
    agent = _agent(brain, tmp_path, desk, memory=_memory(mem))
    _run(agent, "tell sam omw")
    assert desk.sent == [text]                                      # once, on the second response
    assert agent._trace[1]["why"] == "not run: no situation check before a step that lasts"


def test_n0_small_reversible_steps_need_no_check_and_a_window_close_does() -> None:
    from yuki.agent.situation import CheckRecord, consequence, covered

    assert consequence("type_text", {"text": "hi"}) is None
    assert consequence("tab", {"action": "close", "tab": "3"}) is None          # a closed tab comes back
    assert consequence("scroll", {"dy": -3}) is None and consequence("launch_app", {"query": "x"}) is None
    assert consequence("type_text", {"text": "hi", "press_enter": True}, sends=True) == "sends something to a person"
    assert consequence("window_action", {"hwnd": 1, "action": "close"}) == "closes a window"
    assert consequence("run_powershell", {"risk": {"reversible": False, "effect": "deletes the folder"}}) \
        == "cannot be undone (deletes the folder)"
    checks = [CheckRecord(hwnd=CHAT, at=NOW, call=2)]
    assert covered(checks, CHAT, 3) and not covered(checks, CHAT, 2) and not covered(checks, PLAYER, 3)
    assert covered([CheckRecord(hwnd=None, at=NOW, call=1)], PLAYER, 2)


# ---------------------------------------------------------------------------
# N2 on the Spotify shape: the slider hand through the dispatcher; shell mouse injection is the judge's to hold
# ---------------------------------------------------------------------------


def _ids(tree: dict[str, Any]) -> dict[str, int]:
    return {e["name"]: e["id"] for e in tree["elements"]}


def test_n2_the_slider_hand_sets_spotifys_own_volume_through_the_bar_that_hides_it() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk(volume=30.0)
    d = Dispatcher(desk)
    looked = d.dispatch("look_at_window", {"hwnd": PLAYER})
    assert looked.ok and "Change volume" in looked.content[0]["text"]
    ids = _ids(desk.get_window_tree(PLAYER))
    out = d.dispatch("set_slider", {"element": ids["Change volume"], "expect_hwnd": PLAYER, "to": 80})
    assert out.ok and desk.volume == 80.0 and "30% -> 80%" in out.summary and "read back" in out.summary
    again = d.dispatch("set_slider", {"element": ids["Change volume"], "expect_hwnd": PLAYER, "by": 30})
    assert again.ok and desk.volume == 100.0
    top = d.dispatch("set_slider", {"element": ids["Change volume"], "expect_hwnd": PLAYER, "by": 10})
    assert top.ok and top.payload["details"]["noop"] and "already at 100%" in top.summary
    assert not any(c[0] in ("run_powershell", "click", "hover") for c in desk.calls)   # no mouse, no script


def test_n2_n4_the_judge_prompt_holds_shell_mouse_and_key_injection_and_points_to_the_hands() -> None:
    from yuki.agent.judge import JUDGE_PROMPT

    assert "mouse_event" in JUDGE_PROMPT and "set_slider" in JUDGE_PROMPT and "SendKeys" in JUDGE_PROMPT
    assert "I'd rather set the slider directly" in JUDGE_PROMPT


# ---------------------------------------------------------------------------
# N3: a state the done tells must rest on a step of this request
# ---------------------------------------------------------------------------


def test_n3_a_done_claiming_the_in_app_slider_is_maxed_before_any_look_is_bounced(tmp_path: Path) -> None:
    desk = ChatPlayerDesk(volume=30.0)
    claim = {"says": "Spotify's own volume slider is at 100%", "seen_in": "look_at_window", "hwnd": PLAYER}

    def second(params: dict) -> SimpleNamespace:
        seen = _texts(params)
        assert "you have not read that" in seen and "Spotify's own volume slider is at 100%" in seen
        return _response(_use("look_at_window", hwnd=PLAYER))

    def third(params: dict) -> SimpleNamespace:
        assert "Change volume" in _texts(params)
        return _response(_use("done", message="Spotify's own slider is at 30%. Want it higher?", about=[PLAYER],
                              states=[{"says": "Spotify's own slider is at 30%", "seen_in": "look_at_window",
                                       "hwnd": PLAYER}]))

    brain = Brain([_response(_use("done", message="Spotify's own slider is already maxed at 100%.", about=[],
                                  states=[claim])), second, third])
    events = _run(_agent(brain, tmp_path, desk), "the spotify volume is low, turn it up")
    finals = [e.text for e in events if isinstance(e, Final)]
    assert finals == ["Spotify's own slider is at 30%. Want it higher?"]


def test_n3_a_state_seen_only_in_the_mixer_is_accepted_only_as_the_mixer_step() -> None:
    from yuki.agent.loop import Agent

    agent = Agent.__new__(Agent)
    agent._trace = [{"tool": "app_volume", "kind": "action", "ok": True, "verdict": True, "expected": False}]
    agent._evidence = {}
    assert agent._state_gaps([{"says": "Spotify is at 100% in the mixer", "seen_in": "app_volume"}]) == []
    gaps = agent._state_gaps([{"says": "Spotify's own slider is at 100%", "seen_in": "look_at_window",
                               "hwnd": PLAYER}])
    assert gaps and "you have not read that" in gaps[0]


# ---------------------------------------------------------------------------
# N4: a toggle is clicked only toward the state they want, read live first and read back after
# ---------------------------------------------------------------------------


def test_n4_the_lyrics_toggle_is_clicked_only_toward_off_and_verified() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk()
    desk.toggles["Lyrics"] = True
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    lyrics = _ids(desk.get_window_tree(PLAYER))["Lyrics"]
    asked = d.dispatch("click", {"element": lyrics, "expect_hwnd": PLAYER})
    assert not asked.ok and "give toggle_to" in asked.summary and desk.clicks == []
    off = d.dispatch("click", {"element": lyrics, "expect_hwnd": PLAYER, "toggle_to": "off"})
    assert off.ok and desk.toggles["Lyrics"] is False and off.payload["details"]["verified"]
    assert "read back: now off" in off.summary
    again = d.dispatch("click", {"element": lyrics, "expect_hwnd": PLAYER, "toggle_to": "off"})   # a stale read
    assert again.ok and again.payload["details"]["noop"] and desk.clicks == ["Lyrics"]           # not clicked twice


def test_n4_a_hidden_player_bar_is_not_clicked_from_an_old_read_until_hovered_and_read_again() -> None:
    from yuki.agent.tools import Dispatcher

    desk = ChatPlayerDesk()
    desk.toggles.update({"Lyrics": True, "Enter Full screen": True})
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    lyrics = _ids(desk.get_window_tree(PLAYER))["Lyrics"]
    desk.lyrics_full = True                                       # the full-size lyrics view hides the bar now
    hidden = d.dispatch("click", {"element": lyrics, "expect_hwnd": PLAYER, "toggle_to": "off"})
    assert not hidden.ok and "not in a fresh read" in hidden.summary and "Hover" in hidden.summary
    assert desk.clicks == []
    d.dispatch("hover", {"x": 800, "y": 920, "expect_hwnd": PLAYER})
    d.dispatch("look_at_window", {"hwnd": PLAYER})
    lyrics = _ids(desk.get_window_tree(PLAYER))["Lyrics"]
    off = d.dispatch("click", {"element": lyrics, "expect_hwnd": PLAYER, "toggle_to": "off"})
    assert off.ok and desk.toggles["Lyrics"] is False and desk.clicks == ["Lyrics"]


def test_n4_a_raw_click_where_the_latest_read_has_no_control_is_refused() -> None:
    from yuki.agent.tools import Dispatcher, ToolError

    desk = ChatPlayerDesk()
    d = Dispatcher(desk)
    d.dispatch("look_at_window", {"hwnd": CHAT})
    with pytest.raises(ToolError, match="nothing you can click is at"):
        d._check_point_on_control(100, 30, CHAT)
    d._check_point_on_control(900, 1000, CHAT)                    # the message box: a control of the read


# ---------------------------------------------------------------------------
# N10: a task list kept through interjections
# ---------------------------------------------------------------------------


def test_n10_a_done_while_the_list_has_items_left_is_held_and_an_interjection_carries_the_list(tmp_path: Path) -> None:
    desk, mem = _sam(None)
    plan = [{"what": "open Chrome", "state": "done"}, {"what": "open YouTube", "state": "done"},
            {"what": "scroll the recommendations", "state": "doing"},
            {"what": "message Sam on Discord: are you coming on tonight", "state": "todo"}]
    agent_box: dict[str, Any] = {}

    def first(params: dict) -> SimpleNamespace:
        agent_box["agent"].steer("turn the volume up")           # said while the list runs
        return _response(_use("tasks", items=plan), _use("scroll", x=900, y=500, dy=-5))

    def second(params: dict) -> SimpleNamespace:
        seen = _texts(params)
        assert "turn the volume up" in seen and "your list still has, in order" in seen
        assert "message Sam on Discord" in seen.split("your list still has, in order", 1)[1]
        # the interjection is done at once; then (wrongly) a done with the message still to do
        return _response(_use("app_volume", app="Spotify", change=10),
                         _use("done", message="Turned it up.", about=[], states=[]))

    def third(params: dict) -> SimpleNamespace:
        assert "the request did not end: your list still has" in _texts(params)
        return _response(_use("check_situation", action="send Sam: are you coming on tonight", hwnd=CHAT,
                              about="Sam"))

    def fourth(params: dict) -> SimpleNamespace:
        done = [dict(p, state="done") for p in plan]
        return _response(_use("type_text", text="you coming on tonight?", press_enter=True, expect_hwnd=CHAT),
                         _use("tasks", 1, items=done),
                         _use("done", message="Scrolled, turned it up, and asked Sam.", about=[], states=[]))

    agent = _agent(Brain([first, second, third, fourth]), tmp_path, desk, memory=_memory(mem))
    agent_box["agent"] = agent
    events = _run(agent, "open chrome, open youtube, scroll the recommendations, then message sam on discord "
                         "asking if he's coming on tonight")
    assert desk.sent == ["you coming on tonight?"]
    assert [e.text for e in events if isinstance(e, Final)] == ["Scrolled, turned it up, and asked Sam."]
    tasks = [s for s in agent._trace if s["tool"] == "tasks"]
    assert len(tasks) == 2 and "Still to do" in tasks[0]["result"]          # the remaining list is in the trace
    assert [s["tool"] for s in agent._trace].index("check_situation") < \
        [s["tool"] for s in agent._trace].index("type_text")              # the send got its situation check


def test_n10_an_item_is_dropped_only_with_the_users_words() -> None:
    from yuki.agent.tools import Dispatcher

    d = Dispatcher(ChatPlayerDesk())
    bad = d.dispatch("tasks", {"items": [{"what": "message Sam", "state": "dropped"}]})
    assert not bad.ok and "only on their words" in bad.summary
    ok = d.dispatch("tasks", {"items": [{"what": "message Sam", "state": "dropped", "by": "forget the message"}]})
    assert ok.ok and ok.control_input["items"][0]["by"] == "forget the message"


# ---------------------------------------------------------------------------
# N9: the heard form of a name, learned as an alias on the user's confirmation
# ---------------------------------------------------------------------------


def test_n9_a_confirmed_mishearing_is_kept_as_an_alias_only_from_the_users_own_words(tmp_path: Path) -> None:
    mem = FakeMemoryClient(person={"name": "Hemanth", "aka": ["hemanth"], "relation": "best friend", "routes": []})
    brain = Brain([
        _response(_use("about", name="Hemanth", heard_as="hey meant")),
        _response(_use("about", 1, name="Hemanth", heard_as="hair nest")),
        _response(_use("done", message="Got it.", about=[], states=[])),
    ])
    agent = _agent(brain, tmp_path, ChatPlayerDesk(), memory=_memory(mem))
    _run(agent, "yes, hey meant is Hemanth")
    assert mem.aliases == [("hey meant", "Hemanth")]              # "hair nest" was never said: not kept


def test_n9_a_name_split_across_two_words_is_named_even_when_one_word_looks_like_another_contact() -> None:
    from yuki.voice.pipeline import heard_note

    words = [{"word": w, "confidence": c} for w, c in (("Message", 0.99), ("hey", 0.8), ("meant", 0.6),
                                                        ("on", 0.99), ("Discord", 0.99))]
    note, _low, near = heard_note(words, ["Hemanth", "Menta", "Priya"])
    assert "'hey meant' sounds close to Hemanth" in note and "Menta" in note and near >= 2
    assert "'meant' sounds close to Menta" in note


def test_n9_the_talker_and_thinker_are_told_to_judge_names_by_sound_and_situation() -> None:
    from yuki.agent.prompt import VOICE_LAYER
    from yuki.voice.mouth import RECOGNITION_GUIDE

    for text in (VOICE_LAYER, RECOGNITION_GUIDE):
        assert "hey meant" in text and "Hemanth" in text and "best guess first" in text.replace("best guess first",
                                                                                                "best guess first")
    assert "heard_as" in VOICE_LAYER


# ---------------------------------------------------------------------------
# N7: words said while typing or dictating into another app are not for Yuki
# ---------------------------------------------------------------------------


def _focus(text: str, *, name: str = "Ask anything", window: int = 0x3001) -> SimpleNamespace:
    return SimpleNamespace(ok=True, accepts_text=True, is_password=False, role="Edit", name=name, window=window,
                           text_source="text", before=text, after="", value=None)


def test_n7_text_growing_in_another_apps_field_while_they_spoke_marks_dictation() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import StubDesktop

    desk = StubDesktop(action_delay_s=0)
    reads = [_focus("make the pipeline"), _focus("make the pipeline so that yuki checks first")]
    desk.focus_snapshot = lambda timeout_s=0.25, **_: reads.pop(0)
    hands = Hands(desk)
    payload = {"windows": [{"hwnd": 0x3001, "process_name": "Code.exe", "title": "notes.md - Code"}]}
    hands._mark_focus(0x3001, payload)
    hands._focus_line(0x3001)
    assert hands.dictating and hands.dictating["app"] == "Code.exe" and hands.dictating["field"] == "Ask anything"
    # the same field with nothing typed: no dictation
    reads[:] = [_focus("same"), _focus("same")]
    hands._mark_focus(0x3001, payload)
    hands._focus_line(0x3001)
    assert hands.dictating is None


def test_n7_the_dictation_note_replaces_the_named_note_and_says_stay_quiet() -> None:
    from yuki.voice.mouth import volatile_block

    block = volatile_block(named=True, state={"dictating": 'Code.exe "notes.md - Code" (the field Ask anything)'})
    text = block["text"]
    assert "they were typing or dictating into that app" in text and "stay_quiet alone" in text
    assert "these words are said to you" not in text
    assert "these words are said to you" in volatile_block(named=True)["text"]


# ---------------------------------------------------------------------------
# N8: a troubled run's lessons reach the next request on that app in this session
# ---------------------------------------------------------------------------


def test_n8_a_troubled_spotify_run_leaves_lessons_for_the_next_request_on_spotify() -> None:
    from yuki.agent.frame import build, session_lessons

    trace = ([{"kind": "action", "tool": "click", "app": "Spotify.exe", "target": 'Button "Lyrics"', "ok": True,
               "verdict": None, "why": "nothing its report watched could show the effect"}] * 6
             + [{"kind": "control", "tool": "run_powershell", "app": "Spotify.exe", "target": "", "ok": False,
                 "verdict": None, "why": "not run: held by Yuki's check"},
                {"kind": "action", "tool": "set_slider", "app": "Spotify.exe", "target": 'Text "Change volume"',
                 "ok": True, "verdict": True, "why": "its report says it was seen"}])
    lessons = session_lessons(trace, outcome="final", steers=["no, the slider inside spotify"], at=NOW)
    lines = lessons["Spotify.exe"]
    assert any('click on Button "Lyrics" 6 times in one run' in x for x in lines)
    assert any("run_powershell" in x and "held" in x for x in lines)
    assert any('what worked after that: set_slider on Text "Change volume"' in x for x in lines)
    assert any("no, the slider inside spotify" in x for x in lines)
    frame = build({}, lessons=[f"[Spotify.exe] {x}" for x in lines])
    assert "Earlier in this session on the apps in play" in frame.text and "set_slider" in frame.text
    assert session_lessons([{"kind": "action", "tool": "press", "app": "x", "target": "", "ok": True,
                             "verdict": True}], outcome="final") == {}          # a clean run teaches nothing here


def test_n8_the_agent_shows_the_lessons_only_for_apps_in_play() -> None:
    from collections import deque

    from yuki.agent.loop import Agent

    agent = Agent.__new__(Agent)
    agent._session_lessons = deque([(time.time(), {"Spotify.exe": ["7:11 PM: click on Lyrics, 6 times"]})])
    agent._request_app_named = None
    with_spotify = {"windows": [{"hwnd": 1, "process_name": "Spotify.exe"}]}
    assert agent._lessons_for(with_spotify) == ["[Spotify.exe] 7:11 PM: click on Lyrics, 6 times"]
    assert agent._lessons_for({"windows": [{"hwnd": 2, "process_name": "chrome.exe"}]}) == []


def test_n8_apologies_are_short_and_never_blame_the_user() -> None:
    from yuki.agent.prompt import SYSTEM_PROMPT
    from yuki.voice.mouth import MOUTH_PROMPT

    assert "never blaming them" in SYSTEM_PROMPT and "never blaming them" in MOUTH_PROMPT


# ---------------------------------------------------------------------------
# N10 (voice): the thinker line carries what is left; a withdrawal carries the user's words
# ---------------------------------------------------------------------------


def test_n10_the_thinker_line_shows_what_is_left_on_a_running_list() -> None:
    from yuki.voice.bridge import Job, VoiceBridge

    bridge = VoiceBridge.__new__(VoiceBridge)
    import threading as _t

    bridge._lock = _t.Lock()
    job = Job(job_id=4, request="open chrome, youtube, scroll, then message Sam", state="running", lane="worker")
    job.tasks = [{"what": "open Chrome", "state": "done"}, {"what": "message Sam on Discord", "state": "todo"}]
    bridge._jobs, bridge._unsubmitted, bridge._finished = {4: job}, {}, []
    line = bridge.work_line()
    assert "still to do on its list: message Sam on Discord" in line
    job.superseded, job.state, job.finished_at = True, "cancelled", time.monotonic()
    bridge._jobs, bridge._finished = {}, [job]
    assert "its list still had: message Sam on Discord" in bridge.work_line()


def test_n10_a_withdrawal_carries_the_users_words_to_its_line() -> None:
    from yuki.ui.runtime import AgentRuntime

    rt = AgentRuntime.__new__(AgentRuntime)
    rt._withdraw_words = {}
    rt.ui_log = SimpleNamespace(event=lambda *a, **k: None)
    rt.cancel = lambda request_id, reason="cancelled": "running"
    assert rt.withdraw(7, words="forget the message, just play lofi") == "running"
    assert rt.withdrawn_text(7, "Withdrawn: replaced by the user's newer request.") == \
        "Withdrawn: replaced by the user's newer request. (their words: \"forget the message, just play lofi\")"
    assert rt.withdrawn_text(8, "Withdrawn.") == "Withdrawn."


def test_n10_the_talker_keeps_a_list_through_a_quick_interjection() -> None:
    from yuki.voice.mouth import TELL_YUKI, WORK_GUIDE

    assert "a quick thing is never instead" in WORK_GUIDE and "the message to Sam is dropped" in WORK_GUIDE
    assert "instead only when their words drop" in TELL_YUKI["properties"]["intent"]["description"]
