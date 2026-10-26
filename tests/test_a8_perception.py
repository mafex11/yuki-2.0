"""Revamp A8 (the design notes): one opacity measure said as a number and a fact,
"could not be read back" as expected-unverifiable (no escalation), and a vanished window's successor.

Fake model client and stub desks: no desktop, no network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from yuki.agent.loop import EXPECTED_UNVERIFIABLE, Agent, ToolOutcome, action_verdict
from yuki.agent.memory import MemoryAccess
from yuki.agent.tools import Dispatcher, find_successor
from yuki.config import Settings
from yuki.log.events import Final, SessionLogger
from yuki.voice.offline import StubDesktop, StubResult

# -- opacity: one constant, a number and a fact --------------------------------------------------------------


def test_one_opacity_threshold_everywhere():
    from yuki.core import opacity
    from yuki.memory import extract
    from yuki.perception import tree

    assert extract.OPAQUE_COVERAGE is opacity.OPAQUE_COVERAGE and tree.OPAQUE_COVERAGE is opacity.OPAQUE_COVERAGE
    assert opacity.is_opaque(0.03) and not opacity.is_opaque(0.05) and not opacity.is_opaque(None)


def test_opaque_is_a_number_and_never_contradicts_text_that_was_read():
    """1 Oct (01 E4): "OPAQUE - nothing inside it can be read" beside the wizard text captured from that window."""
    from yuki.core.opacity import opacity_fact
    from yuki.memory.now import format_now

    wizard = "Import a project: choose the repositories to import. yuki-2.0 [ ] corvana-core [x] Import"
    board = {"foreground": 7, "windows": [
        {"hwnd": 7, "app": "T3 Code (Alpha)", "live_title": "T3 Code (Alpha)", "kind": "page", "read_at": 1.0,
         "age_s": 40.0, "opaque": True, "coverage": 0.031, "text": wizard, "z": 0, "share": 1.0},
        {"hwnd": 8, "app": "Warp", "live_title": "tab", "read_at": 1.0, "age_s": 5.0, "opaque": True,
         "coverage": 0.0, "text": "", "z": 1, "share": 0.5},
    ], "popups": []}
    text = format_now(board, foreground=7)
    t3 = next(line for line in text.splitlines() if "[7]" in line)
    assert "cover 3%" in t3 and f"{len(wizard)} characters of its text were read" in t3 and wizard in text
    assert "nothing inside it can be read" not in text and "clicks at x,y inside it are refused" in t3
    warp = next(line for line in text.splitlines() if "[8]" in line)
    assert "cover under 1%" in warp and "no text of it was read" in warp
    assert opacity_fact(None, 0) == "opaque: its UI Automation elements cover an unmeasured share of it; no text of it was read"
    assert "1,200 characters" in opacity_fact(0.02, 1200, elements=3)


# -- read-back: expected-unverifiable never escalates -----------------------------------------------------------


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _response(*blocks: SimpleNamespace) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=usage, id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        return self.responses.pop(0)


def _agent(client: FakeClient, tmp: Path, readback: str) -> Agent:
    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)
    agent.dispatcher.backend.type_text = lambda *a, **kw: StubResult(summary="typed", details={"readback": readback})
    return agent


def test_unreadable_field_is_expected_unverifiable():
    out = ToolOutcome(name="type_text", ok=True, summary="typed", payload={"details": {"readback": "unreadable"}})
    assert action_verdict("type_text", out) == (None, EXPECTED_UNVERIFIABLE)
    moved = ToolOutcome(name="type_text", ok=True, summary="typed", payload={"details": {"readback": "moved"}})
    assert action_verdict("type_text", moved)[1] != EXPECTED_UNVERIFIABLE   # focus moved: really unverified


def test_typing_into_an_unreadable_field_neither_escalates_nor_holds_the_done(tmp_path: Path):
    """30 Sept 21:30: "field could not be read back" escalated to medium + thinking: a 53k-token cache rewrite."""
    client = FakeClient([_response(
        _block("tool_use", id="t1", name="type_text", input={"text": "bhai kab aayega", "expect_hwnd": 0x1001}),
        _block("tool_use", id="t2", name="done", input={"message": "Typed it.", "about": []}))])
    agent = _agent(client, tmp_path, "unreadable")
    events = list(agent.run("type bhai kab aayega", effort="low", thinking=False, escalate=("medium", True)))
    assert isinstance(events[-1], Final) and events[-1].text == "Typed it."
    assert len(client.calls) == 1 and agent.last_summary["escalated"] is None


def test_a_finish_on_an_unreadable_field_is_held_for_a_look_but_the_effort_stays(tmp_path: Path):
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"text": "hi", "expect_hwnd": 0x1001, "finishes_with": "Sent."})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []}))])
    agent = _agent(client, tmp_path, "unreadable")
    events = list(agent.run("say hi", effort="low", thinking=False, escalate=("medium", True)))
    assert events[-1].text == "Sent." and len(client.calls) == 2
    second = client.calls[1]
    assert second["thinking"] == {"type": "disabled"} and second["output_config"]["effort"] == "low"
    assert agent.last_summary["escalated"] is None


def test_a_field_that_does_not_hold_the_text_still_escalates(tmp_path: Path):
    client = FakeClient([
        _response(_block("tool_use", id="t1", name="type_text", input={"text": "hi", "expect_hwnd": 0x1001})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Done.", "about": []}))])
    agent = _agent(client, tmp_path, "missing")
    list(agent.run("say hi", effort="low", thinking=False, escalate=("medium", True)))
    assert agent.last_summary["escalated"] is not None
    assert client.calls[1]["thinking"]["type"] == "adaptive"


# -- a vanished window's successor ---------------------------------------------------------------------------


def test_successor_by_process_title_and_place():
    was = {"process": "T3 Code (Alpha).exe", "title": "T3 Code (Alpha)", "rect": [0, 0, 1920, 1040]}
    windows = [
        {"hwnd": 1312414, "process_name": "T3 Code (Alpha).exe", "title": "T3 Code (Alpha)", "bounds": (0, 0, 1920, 1040)},
        {"hwnd": 55, "process_name": "chrome.exe", "title": "T3 Code (Alpha)", "bounds": (0, 0, 1920, 1040)},
    ]
    found = find_successor(was, windows, gone=132798)
    assert found["hwnd"] == 1312414 and found["title_match"] == 1.0 and found["overlap"] == 1.0
    # two windows of the app equally like it: not clear which, so none
    twins = windows + [{"hwnd": 9, "process_name": "T3 Code (Alpha).exe", "title": "T3 Code (Alpha)",
                        "bounds": (0, 0, 1920, 1040)}]
    assert find_successor(was, twins, gone=132798) is None
    # a different window of the same app, elsewhere and named otherwise: not its successor
    other = [{"hwnd": 3, "process_name": "T3 Code (Alpha).exe", "title": "Settings", "bounds": (2000, 0, 2400, 300)}]
    assert find_successor(was, other, gone=132798) is None


class GoneDesk(StubDesktop):
    """T3 Code's window 100 was re-created as 200 (the 1 Oct reload)."""

    def __init__(self, alive: set[int]) -> None:
        super().__init__(action_delay_s=0)
        self.alive = alive

    def window_exists(self, hwnd: int) -> bool:
        return int(hwnd) in self.alive

    def get_desktop_overview(self) -> dict[str, Any]:
        return {"windows": [{"hwnd": h, "process_name": "T3 Code (Alpha).exe", "title": "T3 Code (Alpha)",
                             "is_foreground": True, "is_minimized": False, "pid": 9, "bounds": (0, 0, 1920, 1040)}
                            for h in sorted(self.alive)],
                "foreground_hwnd": min(self.alive) if self.alive else None, "cursor": (0, 0),
                "screen_size": (1920, 1080), "captured_at": 0.0, "monitors": [], "virtual_bounds": (0, 0, 1920, 1080)}


def test_a_call_on_a_vanished_window_runs_on_its_successor_and_says_so():
    desk = GoneDesk({100})
    dispatcher = Dispatcher(desk)
    dispatcher.overview_text()                            # the window as it was: process, title, place
    desk.alive = {200}
    outcome = dispatcher.dispatch("focus_window", {"hwnd": 100})
    assert desk.calls[-1][0] == "focus_window" and desk.calls[-1][1][0] == 200
    note = outcome.content[0]["text"]
    assert "Window 100 is gone" in note and "[200]" in note and "100% alike" in note
    assert outcome.payload["successor"]["to"] == 200
    # nothing took its place: a plain error saying so, nothing run
    desk.alive = set()
    gone = dispatcher.dispatch("focus_window", {"hwnd": 200})
    assert not gone.ok and "no longer exists" in gone.summary and "look_at_desktop" in gone.summary
    # a window that still exists is untouched
    desk.alive = {200}
    calls = len(desk.calls)
    ok = dispatcher.dispatch("focus_window", {"hwnd": 200})
    assert desk.calls[calls][1][0] == 200 and "successor" not in (ok.payload or {})
