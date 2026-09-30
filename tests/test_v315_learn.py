"""v3.15: a long or troubled job ends with what it learned, kept as the app's know-how and handed to the next
request in that app; and Yuki can open its own window. Fake model client, stub desk, a temp memory database."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from yuki.agent.loop import Agent
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import Final, SessionLogger
from yuki.voice.offline import StubDesktop, StubResult

DISCORD = 0x2002


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


class DiscordDesk(StubDesktop):
    """Discord in front; every key it is sent shows its effect."""

    def __init__(self) -> None:
        super().__init__(action_delay_s=0)

    def get_desktop_overview(self) -> dict[str, Any]:
        o = super().get_desktop_overview()
        for w in o["windows"]:
            w["is_foreground"] = False
        o["windows"].insert(0, {"hwnd": DISCORD, "process_name": "Discord.exe", "title": "#after-hours - Discord",
                                "is_foreground": True, "is_minimized": False, "pid": 99, "bounds": (0, 0, 1920, 1040)})
        o["foreground_hwnd"] = DISCORD
        return o

    def hotkey(self, *keys: Any, **kw: Any) -> StubResult:
        self.calls.append(("hotkey", keys, kw))
        return StubResult(summary="ok", details={"foreground_changed": True, "foreground_hwnd": DISCORD})


def _memory(tmp: Path) -> tuple[MemoryAccess, Any]:
    from yuki.memory.api import MemoryClient
    from yuki.memory.store import Store

    client = MemoryClient(Store.open(tmp / "memory" / "memory.db"))
    client._embedder = SimpleNamespace(embed=lambda texts, wait_s=None: None)   # no model: app entries only
    return MemoryAccess(open_client=lambda: client), client


def _agent(client: FakeClient, tmp: Path, memory: MemoryAccess) -> Agent:
    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv")
    return Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client, backend=DiscordDesk(),
                 prewarm=False, record_requests=False, memory=memory)


def _texts(params: dict) -> str:
    out = []
    for m in params["messages"]:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        for b in content:
            if isinstance(b, dict):
                out.append(str(b.get("text") or ""))
                for inner in b.get("content") or [] if isinstance(b.get("content"), list) else []:
                    if isinstance(inner, dict):
                        out.append(str(inner.get("text") or ""))
    return "\n".join(out)


def test_a_long_job_ends_with_a_learned_note_kept_for_its_app_and_given_to_the_next_request(tmp_path: Path) -> None:
    learned = "In Discord, open the channel with Ctrl+K, type @ and the name, accept the suggestion with Tab."
    keys = [["ctrl", "k"], ["enter"], ["tab"]]
    client = FakeClient([
        *[_response(_block("tool_use", id=f"t{i}", name="hotkey", input={"keys": k})) for i, k in enumerate(keys)],
        _response(_block("tool_use", id="d1", name="done", input={"message": "Sent it.", "about": []})),
        _response(_block("tool_use", id="d2", name="done", input={"message": "Sent it.", "about": [],
                                                                 "learned": learned})),
    ])
    memory, store_client = _memory(tmp_path)
    agent = _agent(client, tmp_path, memory)
    events = list(agent.run("message Hemanth in after-hours", effort="low", thinking=False,
                            escalate=("medium", True), voice=True, lean=True))
    assert isinstance(events[-1], Final) and events[-1].text == "Sent it."
    assert len(client.calls) == 5
    # The third action's result says the note is due; the done without it is sent back once, not as an error,
    # and nothing escalates for it (the last call is still the fast pass).
    assert "fill done's learned" in _texts(client.calls[3])
    assert "Not sent yet" in _texts(client.calls[4])
    assert client.calls[4]["thinking"] == {"type": "disabled"} and agent.last_summary["escalated"] is None
    agent._learn_thread.join(10)
    assert any(learned in line and line.startswith("[discord]") for line in store_client.knowhow(app="Discord.exe"))

    # The next spoken request with Discord in front gets it in its memory block, before its first model call.
    nxt = FakeClient([_response(_block("tool_use", id="n1", name="done", input={"message": "Sent.", "about": []}))])
    list(_agent(nxt, tmp_path, memory).run("tag Rohan and ask when he's free", effort="low", thinking=False,
                                           voice=True, lean=True))
    assert learned in _texts(nxt.calls[0])


def test_a_short_clean_job_needs_no_note(tmp_path: Path) -> None:
    client = FakeClient([_response(_block("tool_use", id="t1", name="hotkey", input={"keys": ["ctrl", "k"]}),
                                   _block("tool_use", id="d1", name="done", input={"message": "Done.", "about": []}))])
    memory, _store = _memory(tmp_path)
    events = list(_agent(client, tmp_path, memory).run("open the switcher", effort="low", thinking=False))
    assert events[-1].text == "Done." and len(client.calls) == 1


def test_yuki_opens_its_own_window_on_a_page_from_the_thinker_and_the_talker(tmp_path: Path) -> None:
    from yuki.agent.tools import Dispatcher
    from yuki.ui import own_window
    from yuki.voice.hands import Hands

    opened: list[str] = []
    own_window.register(None)   # a WindowHost another test built registers the real opener: never call it here
    assert not own_window.open_page("today")[0]   # no tray in this process: said so, nothing raised
    own_window.register(lambda page: opened.append(page) or True)
    try:
        outcome = Dispatcher(StubDesktop(action_delay_s=0)).dispatch("open_yuki", {"page": "today"})
        assert outcome.ok and "today" in outcome.summary
        ok, report, facts = Hands(StubDesktop(action_delay_s=0))._run("open_yuki", {"page": "me",
                                                                                    "say_when_done": ""})
        assert ok and facts["verified"] is True
        assert opened == ["today", "me"]
        assert not own_window.open_page("dashboard")[0]   # only the window's own pages
    finally:
        own_window.register(None)
