"""Voice v3.5 (2026-09-26 live sessions): offline checks, no device, no network, no real desktop (rule 7).

The pill follows real job state; say_first is spoken while the call is written; a speculative reply's
tool calls wait for the confirmed turn; one app's own volume through the mixer (a stub mixer); tabs are
closed only by identity and reopened only when verified; a wrong effect stops the talker's hands; one
actor on the desktop at a time; keyterms from memory's facts; the voiced level on a noisy input.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest


# -- the pill: "Working on it" only while a job really runs ---------------------------------------

class _Jobs:
    def __init__(self) -> None:
        self.open = 0
        self.busy_tools = 0
        self.closing = False

    def open_voice_jobs(self) -> int:
        return self.open


def _conversation(jobs: _Jobs) -> Any:
    from yuki.config import Settings
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    states: list[str] = []
    conv = Conversation(settings=Settings(voice_idle_s=30.0), bridge=jobs, transport=None, memory=None,
                        mouth_memory=MouthMemory(), log=SimpleNamespace(event=lambda *a, **k: None, turn=lambda r: None),
                        speech_end=lambda: None, on_state=lambda s, d: states.append(s), session="t")
    conv.observer.idle_state = lambda: "working" if jobs.open_voice_jobs() else "listening"
    return conv, states


def test_pill_says_working_only_while_a_job_runs_and_clears_after() -> None:
    async def main() -> None:
        jobs = _Jobs()
        conv, states = _conversation(jobs)
        conv._loop = asyncio.get_running_loop()
        conv.observer.set_state("listening")  # as Conversation.run does before watching
        runner = SimpleNamespace(cancel=lambda: asyncio.sleep(0))
        watch = asyncio.create_task(conv._watch(runner))
        jobs.open = 1
        conv._activity.set()
        await asyncio.sleep(0.05)
        assert conv.observer.state == "working"
        jobs.open = 0  # the job finished and its result was folded in silently (a remember job, 21:03)
        conv._activity.set()  # the bridge's on_jobs_changed
        await asyncio.sleep(0.05)
        assert conv.observer.state == "listening", states
        watch.cancel()

    asyncio.run(main())


def test_a_reply_with_no_words_does_not_leave_thinking_up() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, TranscriptionFrame

    conv, states = _conversation(_Jobs())
    obs = conv.observer
    obs.on_frame(TranscriptionFrame(text="bro did you see that", user_id="u", timestamp="t"))
    assert obs.state == "thinking"
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMFullResponseEndFrame())  # stay_quiet, or a hand that reports later
    assert obs.state == "listening"


def test_bridge_tells_the_conversation_when_jobs_change() -> None:
    from yuki.voice.bridge import VoiceBridge

    class Runtime:
        def submit(self, text: str, **_: Any) -> tuple[str, int]:
            return "worker", 7

    async def main() -> None:
        bridge = VoiceBridge(Runtime(), hands_backend=None)
        bridge.attach(asyncio.get_running_loop())
        changed: list[int] = []
        bridge.on_jobs_changed = lambda: changed.append(1)
        job = bridge._new_job("x")
        bridge._submit(job, "x", None)
        bridge._finish(job, "done", "ok")
        await asyncio.sleep(0.01)
        assert len(changed) >= 2 and bridge.open_voice_jobs() == 0

    asyncio.run(main())


# -- say_first while the call is written -----------------------------------------------------------

def test_early_words_waits_for_the_whole_string() -> None:
    from yuki.voice.pipeline import early_words

    stream = '{"say_first": "Sure, one sec.", "request": "What have I done today?", "intent": "new_task"}'
    seen = [early_words(stream[:i]) for i in range(len(stream) + 1)]
    first = next(i for i, w in enumerate(seen) if w)
    assert seen[first] == "Sure, one sec." and stream[:first].endswith('one sec."')
    assert early_words('{"request": "x"') is None and early_words('{"say_first": "Sure') is None
    assert early_words('{"say_first": "say \\"hi\\"", ') == 'say "hi"'


# -- a speculative reply's tool calls wait for the confirmed turn ----------------------------------

class _Gate:
    def __init__(self) -> None:
        self.is_speculating = True


class _Base:
    def __init__(self, *a: Any, **k: Any) -> None:
        self._speculation_gate = _Gate()
        self.ran: list[Any] = []
        self.pushed: list[Any] = []
        self.tasks: list[Any] = []

    async def run_function_calls(self, calls: Any) -> None:
        self.ran.append(list(calls))

    async def push_frame(self, frame: Any, direction: Any = None) -> None:
        self.pushed.append(frame)

    def create_task(self, coro: Any, name: str = "") -> Any:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _service() -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    hooks = SimpleNamespace(speculation=lambda op, n: None)
    return _layered_service(_Base, MouthMemory(), hooks)()


def test_speculative_tool_calls_run_on_confirmation_once() -> None:
    from pipecat.frames.frames import UserStoppedSpeakingFrame

    async def main() -> None:
        llm = _service()
        await llm.run_function_calls(["tell_yuki#1"])
        assert llm.ran == []  # held, not withdrawn and asked again
        llm._speculation_gate.is_speculating = False
        await llm.push_frame(UserStoppedSpeakingFrame())
        await asyncio.gather(*llm.tasks)
        await llm.push_frame(UserStoppedSpeakingFrame())  # a second confirmation runs nothing twice
        assert llm.ran == [["tell_yuki#1"]]

    asyncio.run(main())


def test_withdrawn_speculation_drops_its_tool_calls() -> None:
    from pipecat.frames.frames import EagerEndOfTurnCancelFrame, UserStoppedSpeakingFrame

    async def main() -> None:
        llm = _service()
        await llm.run_function_calls(["tell_yuki#1"])
        await llm.push_frame(EagerEndOfTurnCancelFrame())  # the user went on talking
        await llm.push_frame(UserStoppedSpeakingFrame())
        assert llm.ran == [] and not llm.tasks

    asyncio.run(main())


# -- one app's own volume ------------------------------------------------------------------------

def test_app_volume_changes_only_that_app_and_reads_it_back() -> None:
    from yuki.voice.offline import MixerStub

    mixer = MixerStub({"chrome.exe": 100.0, "Spotify.exe": 40.0}, system=45.0)
    result = mixer.app_volume("Google Chrome", change=-20)
    assert result.ok and result.details["verified"] and mixer.levels == {"chrome.exe": 80.0, "Spotify.exe": 40.0}
    assert "system volume was not touched (still 45%)" in result.summary and "Nothing was focused" in result.summary
    assert mixer.app_volume("chrome", level=150).details["after"] == 100.0
    assert mixer.app_volume("spotify.exe", mute=True).ok and mixer.muted == {"Spotify.exe": True}
    missing = mixer.app_volume("youtube", change=-10)
    assert not missing.ok and "chrome.exe" in missing.summary and mixer.levels["chrome.exe"] == 100.0


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


def test_app_volume_hand_leaves_the_window_in_front_and_says_its_line() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()   # qVoice P1 (18 Oct): the app volumes moved to the Situation's Sound line, off the hands' block
    front = desk.front
    done = _hand(hands, "app_volume", app="chrome.exe", change=-20, say_when_done="Turned YouTube down.")
    assert done.spoken == ["Turned YouTube down."] and desk.mixer.levels["chrome.exe"] == 80.0
    assert desk.front == front and desk.volume == 60.0  # no focus change, system volume untouched


def test_real_backend_gets_the_mixer_but_a_stub_never_does() -> None:
    from yuki.agent.tools import _LazyRealBackend
    from yuki.voice.hands import with_app_volume
    from yuki.voice.offline import StubDesktop

    stub = StubDesktop(action_delay_s=0)
    assert with_app_volume(stub) is stub  # its own stub mixer, never the real one
    real = with_app_volume(_LazyRealBackend())
    assert callable(real.app_volume) and callable(real.audio_apps)  # not called here: the real mixer


# -- tabs: closed by identity, reopened only when verified; a wrong effect stops the hands ---------

def test_close_the_named_tabs_never_the_video_or_gmail() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    snap = hands.snapshot()
    assert 'tabs of [' in snap.text and "Claude Platform" in snap.text
    unnamed = _hand(hands, "tab", window=desk.CHROME, action="close", tabs=[7], say_when_done="Closed.")
    assert unnamed.spoken == [] and not desk.closed  # a tab their words do not name is never closed
    # kHands (18 Oct): tabs are named by the words of their titles (named); numbers count only on the tabs line shown
    # and only when those titles fit the words. A word two tabs fit, the video one of them, is asked about by title,
    # never guessed
    hands.new_user_turn()
    several = _hand(hands, "tab", window=desk.CHROME, action="close", named="YouTube", say_when_done="Closed.")
    assert several.spoken == [] and not desk.closed and "2 tabs fit those words" in str(several.result)
    hands.new_user_turn()
    done = _hand(hands, "tab", window=desk.CHROME, action="close", tabs=[3, 4], named="Claude",
                 say_when_done="Closed them.")
    assert done.spoken == ["Closed them."]
    hands.new_user_turn()
    last = _hand(hands, "tab", window=desk.CHROME, action="close", named="ElevenLabs",
                 say_when_done="Closed ElevenLabs.")
    assert last.spoken == ["Closed ElevenLabs."]
    assert [t["name"] for t in desk.closed] == ["Sign in successful | Claude Platform", "Claude",
                                                "Settings | ElevenLabs"]
    names = [t["name"] for t in desk.tabs]
    assert "Wasureranneyo - Nannmonee Music Video - YouTube" in names and any("Inbox" in n for n in names)
    assert desk.tabs[desk.selected]["name"].startswith("Wasureranneyo")  # the video still showing


def test_a_tab_that_moved_is_closed_by_identity_and_a_gone_one_is_refused() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()
    desk.tabs.insert(0, {"name": "New", "url": "x", "scroll": 0.0, "titled_at": 0.0, "id": "42.9"})  # moved
    # kHands (18 Oct): the number is read on the tabs line shown, with the words of its title in named
    _hand(hands, "tab", window=desk.CHROME, action="close", tab=6, named="ElevenLabs", say_when_done="Closed.")
    assert desk.closed[-1]["name"] == "Settings | ElevenLabs"  # not whatever is 6th now
    hands.new_user_turn()
    again = _hand(hands, "tab", window=desk.CHROME, action="close", tab=6, named="ElevenLabs",
                  say_when_done="Closed.")
    assert again.spoken == [] and len(desk.closed) == 1  # that tab is gone: nothing else is closed
    assert "no tab's title holds those words" in str(again.result)   # kHands' refusal: nothing guessed in its place


def test_reopen_is_verified_by_title_and_a_wrong_tab_stops_the_hands(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.voice.hands as hands_module
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    # Phase 2 (18 Oct): after a wrong effect the hands may try other ways for RETRY_BUDGET_S; with none left the next
    # call is "not tried" (the old "stay still until the user speaks" wording went with it)
    monkeypatch.setattr(hands_module, "RETRY_BUDGET_S", 0)
    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.snapshot()
    desk.tab(desk.CHROME, "42.2492528.4.4", "close")  # the video was closed ...
    desk.tab(desk.CHROME, "42.2492528.4.3", "close")  # ... and then a Claude tab (the most recent)
    hands.snapshot()
    wrong = _hand(hands, "tab", window=desk.CHROME, action="reopen",
                  expect="Wasureranneyo - Nannmonee Music Video - YouTube", say_when_done="It's back.")
    assert wrong.spoken == [] and "NOT the one wanted" in str(wrong.result)
    blocked = _hand(hands, "tab", window=desk.CHROME, action="reopen",
                    expect="Wasureranneyo - Nannmonee Music Video - YouTube", say_when_done="It's back.")
    assert "Not tried" in str(blocked.result)  # never repeated blindly
    assert [t["name"] for t in desk.tabs].count("Claude") == 1  # only the one reopen happened
    hands.new_user_turn()
    right = _hand(hands, "tab", window=desk.CHROME, action="reopen",
                  expect="Wasureranneyo - Nannmonee Music Video - YouTube", say_when_done="It's back.")
    assert right.spoken == ["It's back."]


def test_window_hands_wait_while_the_thinker_works_but_sound_does_not() -> None:
    from yuki.voice.hands import Hands
    from yuki.voice.offline import TabsDesk

    desk = TabsDesk(action_delay_s=0)
    hands = Hands(desk)
    hands.bridge = SimpleNamespace(desk_job=lambda: 'task "close the Claude tabs"', busy_tools=0, gate=None)
    hands.snapshot()
    refused = _hand(hands, "tab", window=desk.CHROME, action="close", tab=3, named="the Claude tab",
                    say_when_done="Closed.")
    assert "thinker is working on the desktop" in str(refused.result) and not desk.closed
    sound = _hand(hands, "app_volume", app="chrome.exe", change=-10, say_when_done="Quieter.")
    assert sound.spoken == ["Quieter."]


def test_the_talker_has_no_keyboard() -> None:
    from yuki.voice.hands import HAND_NAMES

    assert "press_keys" not in HAND_NAMES


# -- keyterms from memory's own facts ----------------------------------------------------------------

def test_keyterms_are_people_first_short_subjects_only() -> None:
    from yuki.voice.keyterms import terms_from_facts
    from yuki.voice.pipeline import keyterm_list

    facts = [SimpleNamespace(kind="work", subject="Corvana infrastructure (Vercel/Supabase/GitHub)"),
             SimpleNamespace(kind="work", subject="Claude"), SimpleNamespace(kind="person", subject="Tarnesh"),
             SimpleNamespace(kind="person", subject="Rohit"), SimpleNamespace(kind="behaviour", subject="Late night"),
             SimpleNamespace(kind="interest", subject="Driftwood Music / music production")]
    facts.append(SimpleNamespace(kind="work", subject="Speech transcription setup"))
    loops = [SimpleNamespace(person="Robit")]  # the misheard Rohit, as an open loop kept it live
    terms = terms_from_facts(facts, loops)
    assert terms == ["Tarnesh", "Rohit", "Claude"]
    assert keyterm_list(["Yuki", "Kian"], terms + ["yuki"], 4) == ["Yuki", "Kian", "Tarnesh", "Rohit"]


# -- the voiced level on a noisy interface input ----------------------------------------------------

def test_noisy_floor_is_not_all_voiced() -> None:
    import numpy as np

    from yuki.voice.audio import SharedMic

    mic = SharedMic(None)
    rng = np.random.default_rng(1)
    for i in range(300):  # a -32 dBFS noise floor (the M-Audio input, live), then speech at -12 dBFS
        mic.feed((rng.standard_normal(320) * 10 ** (-32 / 20)).astype(np.float32), float(i) * 0.02)
    quiet = mic.stats.voiced_blocks
    for i in range(50):
        mic.feed((rng.standard_normal(320) * 10 ** (-12 / 20)).astype(np.float32), 6.0 + i * 0.02)
    assert quiet < 30 and mic.stats.voiced_blocks - quiet == 50


# -- corrections reach the thinker; ending a conversation is never a rule --------------------------

def test_a_correction_travels_with_the_request() -> None:
    from yuki.voice.mouth import CORRECTION_MARK, brain_request

    text = brain_request("Reduce the YouTube volume", ["No, you reduced the system volume"],
                         corrects="my hand pressed volumedown (system volume) for 'reduce the YouTube volume'",
                         done_before="20:56:13 your hand: media_key key=volumedown times=3 -> verified")
    assert CORRECTION_MARK in text and "volumedown" in text and "Done on the desktop" in text


def test_end_conversation_ends_it_and_saves_nothing() -> None:
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.mouth import END_CONVERSATION, REMEMBER_NOTE

    # ge (21 Oct): END_CONVERSATION says "Tasks go on; nothing is saved." (its "not a rule" wording went)
    assert "nothing is saved" in END_CONVERSATION["description"] and "not a rule" in REMEMBER_NOTE

    async def main() -> None:
        bridge = VoiceBridge(SimpleNamespace(), hands_backend=None)
        ended: list[int] = []
        bridge.end_requested = lambda: ended.append(1)
        bridge.said_early = lambda call_id: True
        got: list[Any] = []

        async def result_callback(result: Any, properties: Any = None) -> None:
            got.append((result, properties.run_llm))

        await bridge.end_conversation(SimpleNamespace(arguments={"say_first": "Later!"}, tool_call_id="c",
                                                      result_callback=result_callback, llm=None))
        assert ended == [1] and got[0][1] is False

    asyncio.run(main())


def test_a_result_is_always_said_stay_quiet_is_refused_once() -> None:
    from yuki.voice.bridge import VoiceBridge

    async def main() -> None:
        bridge = VoiceBridge(SimpleNamespace(), hands_backend=None)
        bridge.user_turn = lambda: False  # a run a result started
        bridge.said_owed_at = time.monotonic()
        got: list[Any] = []

        async def result_callback(result: Any, properties: Any = None) -> None:
            got.append(result)

        params = SimpleNamespace(arguments={}, tool_call_id="q", result_callback=result_callback)
        await bridge.stay_quiet(params)
        await bridge.stay_quiet(params)
        assert "not_quiet" in got[0] and got[1] == {"ok": True}

    asyncio.run(main())


def test_one_turn_end_is_resolved_once_even_when_two_arrive_together() -> None:
    from yuki.voice.pipeline import one_stop_per_turn

    class Eager:
        def __init__(self) -> None:
            self.calls = 0

        async def handle_user_turn_started(self) -> None:
            pass

        async def trigger_user_turn_stopped(self, **_: Any) -> None:
            await asyncio.sleep(0.01)  # it awaits inside (stopping its speculation timer)
            self.calls += 1

    async def main() -> None:
        stop = one_stop_per_turn(Eager)()
        await asyncio.gather(stop.trigger_user_turn_stopped(), stop.trigger_user_turn_stopped())
        assert stop.calls == 1  # the live race ran two inferences here
        await stop.handle_user_turn_started()
        await stop.trigger_user_turn_stopped()
        assert stop.calls == 2

    asyncio.run(main())


def test_chat_events_follow_what_was_heard_and_a_barge_in_cuts_it() -> None:
    from pipecat.frames.frames import BotStartedSpeakingFrame, InterruptionFrame, TranscriptionFrame, TTSTextFrame

    conv, _states = _conversation(_Jobs())
    obs = conv.observer
    events: list[dict] = []
    obs.on_chat = events.append
    out = object()
    obs.output_processor = out
    obs.on_frame(TranscriptionFrame(text="what's the time", user_id="u", timestamp="t"))
    obs.on_frame(BotStartedSpeakingFrame())
    for word in ("It's", "ten", "past"):
        obs.on_played(SimpleNamespace(frame=TTSTextFrame(text=word, aggregated_by="word"), source=out))
    obs.on_played(SimpleNamespace(frame=TTSTextFrame(text="four", aggregated_by="word"), source=None))  # not yet heard
    obs.on_played(SimpleNamespace(frame=InterruptionFrame(), source=None))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "user_utterance" and events[0]["text"] == "what's the time"
    last = events[-1]
    assert last["type"] == "assistant_speech" and last["final"] and last["interrupted"] and last["text"] == "It's ten past"


def test_hover_reports_what_appeared_without_touching_the_real_pointer(monkeypatch: Any) -> None:
    import yuki.actions.hover as hover_mod
    import yuki.actions.input as inp

    moved: list[tuple[int, int]] = []
    # never the real cursor (rule 7); like input._move it says whether the pointer moved (fGate D17: False = refused)
    monkeypatch.setattr(inp, "_move", lambda x, y, why=None: moved.append((x, y)) or True)
    monkeypatch.setattr(inp, "_in_screen_bounds", lambda x, y: True)
    monkeypatch.setattr(inp, "_foreground_mismatch", lambda hwnd: None)
    reads = iter([{("Button", "Mute (m)"): (176, 951)},
                  {("Button", "Mute (m)"): (176, 951), ("Slider", "Volume"): (230, 951)}])
    monkeypatch.setattr(hover_mod, "_near", lambda automation, x, y, deadline: next(reads, {}))
    result = hover_mod.hover(176, 951, label="[88] Button \"Mute (m)\"")
    assert moved == [(176, 951)] and result.ok and result.details["verified"]
    assert 'Slider "Volume" at (230,951)' in result.summary


def test_the_thinker_has_hover_and_app_volume_as_actions() -> None:
    from yuki.agent.tools import ACTION_TOOL_NAMES, TOOLS_BY_NAME, Dispatcher
    from yuki.voice.offline import MixerStub, StubDesktop

    assert {"hover", "app_volume"} <= set(ACTION_TOOL_NAMES) and "hover" in TOOLS_BY_NAME
    desk = StubDesktop(action_delay_s=0)
    mixer = MixerStub()
    desk.app_volume = mixer.app_volume
    outcome = Dispatcher(desk)._do_app_volume({"app": "chrome", "change": -30})
    assert outcome.ok and mixer.levels["chrome.exe"] == 70.0


def test_heard_note_names_doubtful_words_and_near_names_only() -> None:
    from yuki.voice.mouth import volatile_block
    from yuki.voice.pipeline import heard_note

    names = ["Tarnesh", "Claude", "ElevenLabs", "Father", "Kavya"]

    def words(text: str, low: dict[str, float] | None = None) -> list[dict]:
        return [{"word": w, "confidence": (low or {}).get(w, 0.99)} for w in text.split()]

    note, low, near = heard_note(words("Tell tar niche on WhatsApp to call me."), names)
    assert near == 1 and "'tar niche' sounds close to Tarnesh" in note
    note, low, near = heard_note(words("Send trash message on WhatsApp.", {"trash": 0.40}), names)
    assert low == 1 and "'trash' (0.40)" in note and "Tarnesh" in note
    for clear in ("Close the Claude tabs and the eleven labs tab as well.", "Increase the system volume a bit.",
                  "So after the compaction this will stay in your memory."):
        assert heard_note(words(clear), names)[0] is None, clear  # a clear command gets no note
    block = volatile_block(heard="'cloud' sounds close to Claude, a name you know")["text"]
    assert "never mentioned to them): 'cloud'" in block and "Speech" not in volatile_block()["text"]


def test_mouth_fails_over_to_the_next_profile_and_logs_it() -> None:
    import anthropic
    import httpx

    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import _layered_service

    class Base:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.calls = 0

        async def _create_message_stream(self, api_call: Any, params: Any) -> Any:
            self.calls += 1  # the primary: throttled every time
            raise anthropic.RateLimitError("slow down", response=httpx.Response(
                429, request=httpx.Request("POST", "https://bedrock")), body=None)

    async def backup_stream() -> Any:
        yield SimpleNamespace(type="message_start")

    used: list[Any] = []
    events: list[tuple] = []

    class Client:
        def __init__(self) -> None:
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        async def create(self, **params: Any) -> Any:
            used.append(params["model"])
            return backup_stream()

    hooks = SimpleNamespace(
        fallbacks=lambda: [{"model": "us.anthropic.claude-haiku", "region": "us-east-1"}],
        sticky_index=lambda n: 0, client_for=lambda region: Client(),
        model_problem=lambda i, t, e, k: events.append(("problem", i, k)),
        model_ok=lambda i, t: events.append(("ok", i)))

    async def main() -> None:
        llm = _layered_service(Base, MouthMemory(), hooks)()
        llm._yuki_call = None
        stream = await llm._create_message_stream(None, {"model": "global.haiku", "stream": True})
        got = [e async for e in stream]
        assert llm.calls == 2 and used == ["us.anthropic.claude-haiku"] and got[0].type == "message_start"
        assert events == [("problem", 0, "retry"), ("problem", 0, "retry"), ("ok", 1)]

    asyncio.run(main())
