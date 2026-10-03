"""Voice v3.3: instant hands, "done" only from a verified effect, a shared action log, no "What's up?".

Each case of the live test of 2026-09-25 18:54-18:57 on a stub desktop
(:class:`yuki.voice.offline.BrowserDesk`), plus the scroll verification on a fake
ScrollPattern and on a real one: a Win32 list box this test creates and owns, read
through UI Automation with read-only calls only. Nothing here touches the live
desktop, the microphone or the speakers (ARCHITECTURE.md rule 7).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.voice.hands import ActionLog, Hands
from yuki.voice.offline import BrowserDesk


class _Params:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.function_name, self.arguments, self.tool_call_id = name, args, f"call-{name}"
        self.results: list[dict[str, Any]] = []
        self.spoken: list[str] = []

        async def push_frame(frame: Any) -> None:
            self.spoken.append(frame.text)

        self.llm = SimpleNamespace(push_frame=push_frame)

    async def result_callback(self, result: dict, properties: Any = None) -> None:
        self.results.append(result)


def _hand(hands: Hands, name: str, **args: Any) -> _Params:
    hands.snapshot()
    params = _Params(name, args)
    asyncio.run(hands.handle(params))
    return params


# -- the live cases, on the stub desktop ------------------------------------------------------------


def test_tab_switch_is_the_tab_hand_and_verified() -> None:
    """"Switch to the video tab": a tab of the tabs line, selected and read back; never a window focus."""
    desk = BrowserDesk(action_delay_s=0)
    hands = Hands(desk)
    snap = hands.snapshot()
    assert 'tabs of [12291] (3, * = the one showing): 1 "Getting the most out of Opus 5.5 - YouTube"; 2*' in snap.text
    p = _hand(hands, "tab", window=desk.CHROME, tab=1, say_when_done="There you go.")
    assert p.spoken == ["There you go."] and desk.selected == 0  # said only once the tab is the one showing
    assert "is now the one showing" in p.results[0]["done"]
    # a tab not on the line is refused, never guessed
    p = _hand(hands, "tab", window=desk.CHROME, tab=9, say_when_done="Done.")
    assert p.spoken == [] and "not on the tabs line" in p.results[0]["failed"]
    # the tabs changed between the block and the call: refused
    hands.snapshot()
    desk.tabs[2]["name"] = "Something else"
    params = _Params("tab", {"window": desk.CHROME, "tab": 3, "say_when_done": "Done."})
    asyncio.run(hands.handle(params))
    assert params.spoken == [] and "the tabs changed" in params.results[0]["failed"]
    # the window hand's description says it never changes the tab
    from yuki.voice.hands import HAND_TOOLS

    window = next(t for t in HAND_TOOLS if t["name"] == "window")
    assert "A window is not a tab" in window["description"]


def test_scroll_with_the_cursor_elsewhere_goes_to_the_page_and_done_only_when_it_moved() -> None:
    desk = BrowserDesk(action_delay_s=0)  # cursor over the tab bar, as live
    hands = Hands(desk)
    p = _hand(hands, "scroll", window=desk.CHROME, direction="down", say_when_done="Done.")
    x, y = desk.calls[-1][1][:2]
    assert (x, y) == (960, 520) and desk.cursor == (1135, 20)  # the window's content, not the cursor
    assert p.spoken == ["Done."] and desk.tabs[1]["scroll"] == 10.0
    # at the top already: not moved, so no "done"; the report says why and nothing is handed on blindly
    p = _hand(hands, "scroll", window=desk.CHROME, direction="up", amount=20, say_when_done="Done.")
    p = _hand(hands, "scroll", window=desk.CHROME, direction="up", say_when_done="Done.")
    assert p.spoken == [] and "already at the top" in p.results[0]["failed"]
    assert "already at the end" in p.results[0]["how_to_say_it"]


def test_a_scroll_that_sent_the_wheel_but_moved_nothing_is_not_done() -> None:
    """The v3.2 behaviour on the same desk: "scrolled" came back, nothing moved; now no done line."""
    desk = BrowserDesk(era="before", action_delay_s=0)
    hands = Hands(desk)
    desk.cursor = (1135, 20)
    p = _hand(hands, "scroll", window=desk.CHROME, direction="down", say_when_done="Done.")
    assert p.spoken == []  # the backend could not say it moved: not verified, not "done"
    assert "not_verified" in p.results[0] and "never that it is done" in p.results[0]["how_to_say_it"]


def test_new_tab_with_youtube_waits_for_the_page_and_records_the_tab() -> None:
    desk = BrowserDesk(action_delay_s=0)
    desk.TITLE_DELAY_S = 0.4
    actions = ActionLog()
    hands = Hands(desk, actions=actions)
    started = time.monotonic()
    p = _hand(hands, "open_url", url="https://www.youtube.com", app="Google Chrome", say_when_done="YouTube's up.")
    waited = time.monotonic() - started
    assert p.spoken == ["YouTube's up."] and waited >= 0.35  # not at the address alone: once it has its title
    done = p.results[0]["done"]
    assert 'it is tab 4 of 4 in window 12291, "YouTube", now showing' in done and "loaded" in done
    line = actions.lines()[-1]
    assert "your hand: open_url url=https://www.youtube.com" in line and "verified" in line and "tab 4 of 4" in line
    # a page that never gets its title is not "up"
    desk2 = BrowserDesk(action_delay_s=0)
    desk2.TITLE_DELAY_S = 60
    hands2 = Hands(desk2)
    import yuki.voice.hands as hmod

    old, hmod._TITLE_WAIT_S = hmod._TITLE_WAIT_S, 0.3
    try:
        p = _hand(hands2, "open_url", url="https://www.youtube.com", say_when_done="YouTube's up.")
    finally:
        hmod._TITLE_WAIT_S = old
    assert p.spoken == [] and "not loaded yet" in p.results[0]["not_verified"]


def test_the_thinker_and_the_mouth_see_the_tab_we_just_opened() -> None:
    """"Click on the new YouTube tab we just opened": the hand's verified record goes to both."""
    from yuki.voice.bridge import VoiceBridge
    from tests.test_voice import FakeRuntime

    desk = BrowserDesk(action_delay_s=0)
    desk.TITLE_DELAY_S = 0.0
    bridge = VoiceBridge(FakeRuntime(), foreground=lambda: None, hands_backend=desk)
    _hand(bridge.hands, "open_url", url="https://www.youtube.com", app="Google Chrome", say_when_done="Up.")
    # the mouth's next desktop block
    snap = bridge.hands.snapshot()
    assert "done on the desktop in this conversation" in snap.text and "tab 4 of 4" in snap.text
    assert '4* "YouTube"' in snap.text
    # the thinker's request
    from yuki.voice.mouth import brain_request

    text = brain_request("Click on the new YouTube tab we just opened", ["click on the new tab we just opened"],
                         done_before=bridge.actions.text())
    assert "Done on the desktop earlier in this conversation" in text and "tab 4 of 4" in text
    # the thinker's own desktop actions go into the same log
    bridge._on_tool_called("worker", 7, "click", {"element": 12, "expect_hwnd": 12291})
    bridge._on_tool_finished("worker", 7, "click", True, "clicked TabItem \"YouTube\"")
    assert "the thinker: click" in bridge.actions.lines()[-1]


def test_volume_key_is_verified_from_the_volume() -> None:
    desk = BrowserDesk(action_delay_s=0)
    hands = Hands(desk)
    p = _hand(hands, "media_key", key="volumedown", times=5, say_when_done="Turned it down.")
    assert p.spoken == ["Turned it down."] and "the volume is now 50.0% (was 60.0%)" in p.results[0]["done"]


def test_scroll_again_is_the_same_hand_twice() -> None:
    desk = BrowserDesk(action_delay_s=0)
    hands = Hands(desk)
    first = _hand(hands, "scroll", window=desk.CHROME, direction="down", say_when_done="Done.")
    again = _hand(hands, "scroll", window=desk.CHROME, direction="down", say_when_done="Done.")
    # v3.14: the same done line again within a minute is not said again (live "Done." x5 in 38 s); both scrolls ran
    assert first.spoken == ["Done."] and again.spoken == [] and desk.tabs[1]["scroll"] == 20.0


def test_instant_hands_are_called_without_words_and_the_slow_ones_with() -> None:
    from yuki.voice.hands import HAND_TOOLS, INSTANT_HANDS
    from yuki.voice.mouth import HANDS_PROMPT, MOUTH_PROMPT

    for tool in HAND_TOOLS:
        says = "no words before it" in tool["description"]
        assert says == (tool["name"] in INSTANT_HANDS), tool["name"]
    assert "made alone, with no words written before it" in MOUTH_PROMPT  # v3.5: every call, say_first carries words
    # v3.17 (H1): the hands' part is its own block, sent whenever the talker has hands
    assert "A repeat" in HANDS_PROMPT and "A tab is not a window" in HANDS_PROMPT


# -- "What's up?": a result is always the newest thing ---------------------------------------------


def _live_context_at_18_56_57() -> list[dict[str, Any]]:
    """The mouth's context when job 5's result arrived (the live session, words stand-ins)."""
    from pipecat.processors.aggregators import async_tool_messages as atm

    started5 = atm.build_started_message("job5")
    final4 = {"role": "developer", "content": json.dumps({"type": "async_tool", "status": "finished",
                                                           "tool_call_id": "job4", "result": "{}"})}
    return [
        {"role": "user", "content": "No. No. Not on this YouTube tab. Go to the new one."},
        {"role": "assistant", "tool_calls": [{"id": "job5", "type": "function",
                                              "function": {"name": "tell_yuki", "arguments": "{}"}}]},
        started5,
        {"role": "assistant", "content": "Right, got it, the new YouTube tab."},
        final4,
        {"role": "assistant", "tool_calls": [{"id": "quiet", "type": "function",
                                              "function": {"name": "stay_quiet", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "quiet", "content": json.dumps({"ok": True})},
        {"role": "assistant", "content": "Scrolled down on the new YouTube tab for you."},
    ]


def test_a_late_result_is_appended_not_settled_in_the_middle() -> None:
    from pipecat.frames.frames import FunctionCallInProgressFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator

    from yuki.voice.pipeline import settle_only_when_newest

    frame = FunctionCallInProgressFrame(function_name="tell_yuki", tool_call_id="job5", arguments={},
                                        cancel_on_interruption=False)
    stock = LLMAssistantAggregator(LLMContext(messages=_live_context_at_18_56_57()))
    assert stock._is_deferred(frame) is False  # Pipecat 1.11: settled in place, behind later messages
    ours = LLMAssistantAggregator(LLMContext(messages=_live_context_at_18_56_57()))
    settle_only_when_newest(ours)
    assert ours._is_deferred(frame) is True  # appended: the newest thing the mouth sees
    # a result whose placeholder is still the newest message settles in place, as before
    fresh = LLMAssistantAggregator(LLMContext(messages=_live_context_at_18_56_57()[:3]))
    settle_only_when_newest(fresh)
    assert fresh._is_deferred(frame) is False
    # a synchronous call is untouched
    sync = FunctionCallInProgressFrame(function_name="x", tool_call_id="job5", arguments={},
                                       cancel_on_interruption=True)
    assert ours._is_deferred(sync) is False


def test_the_context_summary_is_not_what_the_user_said() -> None:
    from yuki.voice.bridge import heard_words

    summary = {"role": "user", "content": "Conversation summary: the user wants..."}
    messages = [summary, {"role": "user", "content": "Scroll down again."}]
    context = SimpleNamespace(get_messages=lambda: messages)
    assert heard_words(context, skip=lambda m: m is summary) == ["Scroll down again."]


def test_a_result_names_the_request_it_answers() -> None:
    from yuki.voice.mouth import result_payload

    payload = result_payload("done", "Scrolled down on that tab.", request="Click on the new YouTube tab and scroll")
    assert payload["for_request"] == "Click on the new YouTube tab and scroll"


# -- scroll verification ------------------------------------------------------------------------------


class _FakeScroll:
    """A ScrollPattern stand-in: ``lag`` polls before a new position shows (Chrome is asynchronous)."""

    role, name = "Document", "YouTube"

    def __init__(self, percent: float = 0.0, view: float = 20.0, moves: bool = True, lag: int = 2,
                 settable: bool = True) -> None:
        self.p, self.v, self.moves, self.lag, self.settable = percent, view, moves, lag, settable
        self.pending: float | None = None
        self.reads = 0
        self.steps: list[tuple[bool, bool]] = []

    def percent(self, vertical: bool) -> float:
        self.reads += 1
        if self.pending is not None and self.reads > self.lag:
            self.p, self.pending = self.pending, None
        return self.p

    def view(self, vertical: bool) -> float:
        return self.v

    def set_percent(self, vertical: bool, value: float) -> None:
        if not self.settable:
            raise NotImplementedError
        self.reads = 0
        if self.moves:
            self.pending = value

    def step(self, vertical: bool, forward: bool, large: bool) -> None:
        self.steps.append((forward, large))
        if self.moves:
            self.reads, self.pending = 0, self.p + (25.0 if large else 2.0) * (1 if forward else -1)


def test_scroll_pattern_moves_a_screenful_per_ten_notches_and_is_verified() -> None:
    from yuki.actions.scrolling import describe, scroll_pattern_target, screen_percent

    target = _FakeScroll(percent=10.0, view=20.0)
    out = scroll_pattern_target(target, vertical=True, notches=10, forward=True, poll_s=0.001)
    assert out.moved is True and out.how == "pattern" and out.after == pytest.approx(10.0 + screen_percent(20.0))
    ok, text = describe(out, direction="down", notches=10, x=1, y=1)
    assert ok and 'scrolled Document "YouTube" down: 10% -> 35%' in text and "cursor was not moved" in text


def test_scroll_pattern_that_does_not_move_is_a_failure() -> None:
    from yuki.actions.scrolling import describe, scroll_pattern_target

    out = scroll_pattern_target(_FakeScroll(percent=10.0, moves=False), vertical=True, notches=5, forward=True,
                                watch_s=0.05, poll_s=0.001)
    assert out.moved is False
    ok, text = describe(out, direction="down", notches=5, x=1, y=1)
    assert not ok and "did not move (still 10%)" in text


def test_scroll_at_the_end_says_so_and_sends_nothing() -> None:
    from yuki.actions.scrolling import describe, scroll_pattern_target

    target = _FakeScroll(percent=100.0)
    out = scroll_pattern_target(target, vertical=True, notches=5, forward=True, poll_s=0.001)
    assert out.at_end and out.moved is False and target.pending is None
    assert describe(out, direction="down", notches=5, x=1, y=1) == (
        False, 'Document "YouTube" is already at the bottom (100%): nothing to scroll down')


def test_scroll_pattern_without_set_percent_steps_instead() -> None:
    from yuki.actions.scrolling import scroll_pattern_target

    target = _FakeScroll(percent=0.0, settable=False, lag=0)
    out = scroll_pattern_target(target, vertical=True, notches=10, forward=True, poll_s=0.001)
    assert out.how == "pattern-steps" and out.moved is True and target.steps == [(True, True)]


def test_wheel_fallback_puts_the_cursor_back_and_checks_the_content_moved() -> None:
    from yuki.actions.scrolling import wheel_at

    moves: list[tuple[int, int]] = []
    state = {"top": 500, "sent": 0}

    def send() -> None:
        state["sent"] += 1
        state["top"] -= 300

    out = wheel_at(960, 520, send=send, marker=lambda: state["top"], cursor=lambda: (1135, 20),
                   move=lambda x, y: moves.append((x, y)), poll_s=0.001)
    assert out.moved is True and out.shift_px == 300 and moves == [(960, 520), (1135, 20)]
    # nothing under the point moved (the wheel went to something that does not scroll)
    moves.clear()
    out = wheel_at(960, 520, send=lambda: None, marker=lambda: 500, cursor=lambda: (1135, 20),
                   move=lambda x, y: moves.append((x, y)), watch_s=0.05, back_after_s=0.01, poll_s=0.001)
    assert out.moved is False and moves == [(960, 520), (1135, 20)]


class _OwnListBox:
    """A Win32 list box this test creates (off screen, never activated) and pumps; nobody else's window."""

    def __init__(self, rows: int = 200) -> None:
        self.hwnd = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._rows = rows
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _run(self) -> None:
        u = ctypes.windll.user32
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.restype = ctypes.c_ssize_t
        no_activate, tool_window = 0x08000000, 0x00000080
        popup, vscroll, visible = 0x80000000, 0x00200000, 0x10000000
        hwnd = u.CreateWindowExW(no_activate | tool_window, "LISTBOX", "yuki scroll test", popup | vscroll | visible,
                                 -3000, -3000, 200, 150, None, None, None, None)
        for i in range(self._rows):
            text = ctypes.create_unicode_buffer(f"row {i}")
            u.SendMessageW(hwnd, 0x0180, 0, ctypes.cast(text, ctypes.c_void_p).value)  # LB_ADDSTRING
        self.hwnd, self._u = int(hwnd), u
        self._ready.set()
        msg = wintypes.MSG()
        while not self._stop.is_set():
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)
        u.DestroyWindow(hwnd)

    def show_row(self, row: int) -> None:
        """Move the test's own list (LB_SETTOPINDEX), as a user scrolling it would."""
        self._u.PostMessageW(self.hwnd, 0x0197, row, 0)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)


def test_scroll_verification_reads_a_real_scroll_pattern() -> None:
    """UI Automation's own ScrollPattern on a window this test owns: read-only calls only."""
    box = _OwnListBox()
    try:
        from yuki.actions.input import _Pending
        from yuki.actions.scrolling import _as_target, screen_percent

        seen: dict[str, Any] = {}

        def job(automation: Any, shared: dict) -> None:
            target = _as_target(automation.ElementFromHandle(box.hwnd), True)
            shared["role"] = target.role
            shared["before"] = target.percent(True)
            shared["view"] = target.view(True)
            box.show_row(60)
            deadline = time.monotonic() + 2.0
            after = target.percent(True)
            while after == shared["before"] and time.monotonic() < deadline:
                time.sleep(0.02)
                after = target.percent(True)
            shared["after"] = after

        pending = _Pending(job, "test-scroll-read", 6.0)
        # This thread keeps its own messages moving while it waits (another test may have made it a
        # Qt thread; UI Automation can call back into it).
        u, msg = ctypes.windll.user32, wintypes.MSG()
        deadline = time.monotonic() + 6.0
        while pending._thread.is_alive() and time.monotonic() < deadline:
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
        seen = pending.wait()
        assert "error" not in seen, seen.get("error")
        assert seen["role"] == "List" and seen["before"] == 0.0 and 0 < seen["view"] < 100
        assert seen["after"] > seen["before"]  # the position read back shows the move
        assert screen_percent(seen["view"]) > 0
    finally:
        box.close()


# -- tabs through UI Automation, on a tab control this test owns ---------------------------------------


class _TCITEMW(ctypes.Structure):
    _fields_ = [("mask", wintypes.UINT), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("pszText", wintypes.LPWSTR), ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
                ("lParam", wintypes.LPARAM)]


class _OwnTabs:
    """A Win32 tab control this test creates (off screen, never activated) and pumps; nobody else's window."""

    def __init__(self, names: list[str]) -> None:
        self.names, self.hwnd = names, 0
        self._ready, self._stop = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _run(self) -> None:
        u = ctypes.windll.user32
        ctypes.windll.comctl32.InitCommonControls()
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.restype = ctypes.c_ssize_t
        hwnd = u.CreateWindowExW(0x08000000 | 0x80, "SysTabControl32", "yuki tab test", 0x80000000 | 0x10000000,
                                 -3000, -3000, 400, 60, None, None, None, None)
        items = []
        for i, name in enumerate(self.names):
            item = _TCITEMW(mask=1, pszText=name)
            items.append(item)
            u.SendMessageW(hwnd, 0x133E, i, ctypes.addressof(item))  # TCM_INSERTITEMW
        self.hwnd = int(hwnd)
        self._ready.set()
        msg = wintypes.MSG()
        while not self._stop.is_set():
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)
        u.DestroyWindow(hwnd)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)


def _pumped(fn: Any) -> Any:
    """``fn()`` on a helper thread while this thread keeps its own messages moving."""
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    thread.start()
    u, msg = ctypes.windll.user32, wintypes.MSG()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)
    return box.get("value")


def test_tabs_are_read_and_one_is_selected_and_verified_through_ui_automation() -> None:
    from yuki.actions.tabs import select_tab
    from yuki.perception.tabs import read_tabs, tabs_line

    own = _OwnTabs(["Video", "GitHub", "Inbox"])
    try:
        strip = _pumped(lambda: read_tabs(own.hwnd, budget_s=3.0))
        assert [(t.index, t.name, t.selected) for t in strip.tabs] == [
            (1, "Video", True), (2, "GitHub", False), (3, "Inbox", False)]
        assert tabs_line(strip).endswith('1* "Video"; 2 "GitHub"; 3 "Inbox"')
        result = _pumped(lambda: select_tab(own.hwnd, 3, expect_name="Inbox"))
        assert result.ok and result.details["verified"] and result.details["how"] == "SelectionItem.Select"
        assert _pumped(lambda: read_tabs(own.hwnd, budget_s=3.0)).selected.name == "Inbox"
        stale = _pumped(lambda: select_tab(own.hwnd, 2, expect_name="Video"))
        assert not stale.ok and "the tabs changed" in stale.summary
    finally:
        own.close()
