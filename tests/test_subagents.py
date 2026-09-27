"""Read-only helpers (yuki.agent.subagents): parallel, capped, and unable to touch the desktop.

A stub model client (scripted per task, a fixed latency per call), a stub memory (recall with a fixed latency)
and a stub desktop that records any call. Nothing here reaches the network, the desktop or real memory.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.agent.subagents import HelperTeam, ReadOnlyBackend, report_text
from yuki.agent.tools import ToolOutcome
from yuki.config import Settings

MODEL_S = 0.15     # one stub model round trip
RECALL_S = 0.25    # one stub memory lookup


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _response(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=300, output_tokens=40, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model_dump=lambda **_: {})


class HelperModel:
    """Per helper: first a tool call chosen from the task, then a short report from the tool's result."""

    def __init__(self, first_call: dict[str, tuple[str, dict]]) -> None:
        self.first_call = first_call
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        with self.lock:
            self.calls.append(params)
        time.sleep(MODEL_S)
        messages = params["messages"]
        task = messages[0]["content"]
        if len(messages) == 1:
            name, tool_input = self.first_call[task]
            return _response(_block("tool_use", id=f"t-{abs(hash(task))}", name=name, input=tool_input))
        result = messages[-1]["content"][0]
        text = " ".join(b.get("text", "") for b in result["content"] if isinstance(b, dict))
        return _response(_block("text", text=f"Found: {text[-80:]}"), stop="end_turn")


class Memory:
    installed = True

    def __init__(self) -> None:
        self.asked: list[dict] = []

    def run_tool(self, name: str, tool_input: dict, *, request: str | None = None) -> ToolOutcome:
        time.sleep(RECALL_S)
        self.asked.append({"name": name, **tool_input})
        return ToolOutcome(name=name, ok=True, summary="2 facts",
                           content=[{"type": "text", "text": f"2026-09-26 fact about {tool_input.get('query')}"}])


class Desk:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_desktop_overview(self) -> dict:
        self.calls.append("get_desktop_overview")
        return {"windows": [], "foreground_hwnd": None}

    def format_overview(self, o: Any) -> str:
        return "(no windows)"

    def click(self, *a: Any, **k: Any) -> Any:
        self.calls.append("click")
        raise AssertionError("a helper clicked")

    def run_powershell(self, *a: Any, **k: Any) -> Any:
        self.calls.append("run_powershell")
        raise AssertionError("a helper ran a command")


TASKS = {
    "When did Varun last message the user?": ("recall", {"query": "Varun message"}),
    "What was the Corvana deck about?": ("recall", {"query": "Corvana deck"}),
    "Who is Ravi?": ("recall", {"query": "Ravi"}),
}


def _settings(**kw: Any) -> Settings:
    return Settings(**{"helpers_timeout_s": 20.0, **kw})


def test_three_lookups_and_a_file_read_run_in_parallel_faster_than_serially(tmp_path: Path):
    notes = tmp_path / "notes.txt"
    notes.write_text("Deskflow controls the MacBook from this PC.\n", encoding="utf-8")
    first = dict(TASKS)
    first[f"Read {notes} and say what it controls."] = ("read_file", {"path": str(notes)})
    tasks = list(first)

    model, memory = HelperModel(first), Memory()
    started = time.perf_counter()
    results = HelperTeam(client=model, backend=Desk(), memory=memory, settings=_settings(),
                         nonce="n0nce").run(tasks)
    parallel_s = time.perf_counter() - started
    assert [r.ok for r in results] == [True] * 4 and all(r.stopped == "answered" for r in results)
    assert "Deskflow" in results[3].report and len(memory.asked) == 3

    serial_model, serial_memory = HelperModel(first), Memory()
    started = time.perf_counter()
    for task in tasks:
        HelperTeam(client=serial_model, backend=Desk(), memory=serial_memory, settings=_settings(),
                   nonce="n0nce").run([task])
    serial_s = time.perf_counter() - started
    # each helper: two model calls + one lookup (~0.55 s); four of them: ~2.2 s one by one, ~0.6 s together
    assert parallel_s < serial_s * 0.5, (parallel_s, serial_s)
    print(f"helpers: parallel {parallel_s:.2f} s vs serial {serial_s:.2f} s")


def test_helpers_cannot_use_input_or_desktop_actions(tmp_path: Path):
    desk, memory = Desk(), Memory()
    first = {"Click the send button.": ("click", {"x": 1, "y": 2}),
             "Clean the temp folder.": ("run_powershell", {"command": "Remove-Item C:\\temp\\*"})}
    results = HelperTeam(client=HelperModel(first), backend=desk, memory=memory, settings=_settings(),
                         nonce="n").run(list(first))
    assert "click" not in desk.calls and "run_powershell" not in desk.calls
    tool_steps = [t for r in results for t in r.transcript if "tool" in t]
    assert [t["ok"] for t in tool_steps] == [False, False]
    assert all("not available to helpers" in t["summary"] for t in tool_steps)
    # the backend itself refuses anything but reading
    ro = ReadOnlyBackend(desk)
    with pytest.raises(PermissionError):
        ro.click(1, 2)
    with pytest.raises(PermissionError):
        ro.run_powershell("x")
    assert ro.get_desktop_overview() == {"windows": [], "foreground_hwnd": None}


def test_helper_tool_set_is_read_only():
    from yuki.agent.subagents import helper_tools
    from yuki.agent.tools import ACTION_TOOL_NAMES

    names = {t["name"] for t in helper_tools(True)}
    assert names == {"look_at_desktop", "look_at_window", "read_page", "read_tabs", "recall", "activity",
                     "calls", "call_timeline", "read_file", "fetch_url"}
    assert not names & set(ACTION_TOOL_NAMES)
    assert not names & {"remember_how", "correct_memory", "remember_rule", "forget_rule", "todo", "watch_for",
                        "spawn_helpers", "ask_user", "say"}


def test_turn_cap_asks_for_the_report_with_tools_off():
    class Looper:
        def __init__(self) -> None:
            self.params: list[dict] = []
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **params: Any) -> SimpleNamespace:
            self.params.append(params)
            if params.get("tool_choice") == {"type": "none"}:
                return _response(_block("text", text="Partial: found nothing yet."), stop="end_turn")
            return _response(_block("tool_use", id=f"t{len(self.params)}", name="recall", input={"query": "x"}))

    looper = Looper()
    results = HelperTeam(client=looper, backend=Desk(), memory=Memory(), settings=_settings(helpers_max_turns=3),
                         nonce="n").run(["find x"])
    assert len(looper.params) == 3 and looper.params[-1]["tool_choice"] == {"type": "none"}
    assert results[0].stopped == "turns" and results[0].report.startswith("Partial")
    assert "stopped early: turns cap" in report_text(results)


def test_helper_tool_results_are_fenced_as_untrusted():
    model = HelperModel({"Who is Ravi?": TASKS["Who is Ravi?"]})
    HelperTeam(client=model, backend=Desk(), memory=Memory(), settings=_settings(), nonce="abc123").run(
        ["Who is Ravi?"])
    second = model.calls[1]["messages"][2]["content"][0]   # user turn with the tool result
    assert second["content"][0]["text"].startswith("<<BEGIN_UNTRUSTED_DATA abc123>>")


def test_brain_spawns_helpers_and_the_runtime_sees_their_steps(tmp_path: Path):
    from yuki.agent.loop import Agent
    from yuki.agent.subagents import HelperStep
    from yuki.log.events import Final, SessionLogger

    helper_model = HelperModel(dict(TASKS))
    brain_calls: list[dict] = []

    def create(**params: Any) -> SimpleNamespace:
        if params["model"] != Settings().model:        # a helper's call
            return helper_model.create(**params)
        brain_calls.append(params)
        if len(brain_calls) == 1:
            return _response(_block("tool_use", id="s1", name="spawn_helpers", input={"tasks": list(TASKS)}))
        return _response(_block("tool_use", id="d1", name="done", input={"message": "Here's what I found."}))

    settings = Settings(log_dir=tmp_path / "sessions", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None),
                  client=SimpleNamespace(messages=SimpleNamespace(create=create)), backend=Desk(),
                  prewarm=False, record_requests=False, memory=Memory())
    agent.memory.context = lambda **kw: (_ for _ in ()).throw(RuntimeError("no context in tests"))
    agent.memory.log_turn = lambda **kw: False
    events = list(agent.run("catch me up on Varun, the deck and Ravi"))
    steps = [e for e in events if isinstance(e, HelperStep)]
    assert {s.state for s in steps} == {"started", "tool", "finished"} and len(steps) == 9
    assert isinstance(events[-1], Final)
    result = next(b for b in brain_calls[1]["messages"][-1]["content"] if b.get("type") == "tool_result")
    text = result["content"][0]["text"]
    assert text.startswith("<<BEGIN_UNTRUSTED_DATA") and "Helper 3 - task: Who is Ravi?" in text
    summary = agent.last_summary
    assert "global.anthropic.claude-haiku-4-5-20251001-v1:0" in summary["model"]   # helpers priced in
    assert summary["cost_usd"] > 0
