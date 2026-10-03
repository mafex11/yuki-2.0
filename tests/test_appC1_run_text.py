"""Builder appC1 (revamp 38 §8, the user's 3 Oct feedback 12 and 14 and the return card): every run surface says a step
in plain words from one label function, never a command, typed text, address, key, id or number; the return card says
how a run really ended and shows no undo of any kind.

Names here are invented (Ana, the Example app); nothing comes from the user's memory or logs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

# What a model may put in an action's input that no person should read on a card.
SECRET_INPUT = {
    "command": "Get-Process slack | Stop-Process -Force",
    "text": "hey Ana, see you at 7",
    "url": "https://www.example.com/watch?v=dQw4w9",
    "keys": ["ctrl", "l"],
    "key": "enter",
    "hwnd": 264022,
    "expect_hwnd": 133596,
    "element": 25,
    "read": 3,
    "x": 640,
    "y": 480,
    "tab": "t7",
    "query": r"C:\Tools\run_me.exe",   # a name that looks like data (a path) is left out too
}


def _inputs_for(name: str) -> list[dict[str, Any]]:
    from yuki.agent.tools import TOOLS_BY_NAME

    props = TOOLS_BY_NAME[name]["input_schema"]["properties"]
    base = {k: v for k, v in SECRET_INPUT.items() if k in props}
    out = [base]
    if "action" in props:
        out += [{**base, "action": a} for a in props["action"].get("enum", [])]
    if name == "type_text":
        out.append({**base, "press_enter": True})
    if name == "app_volume":
        out += [{**base, "app": "Spotify.exe", "mute": True}, {**base, "app": "Spotify.exe", "change": -20}]
    # what the agent adds: a control named with digits and an id-like name, an app with a version
    out.append({**base, "_about": {"element": "Inbox (3)", "role": "Button", "app": "Lumen 2026", "tab": "t_7"}})
    out.append({**base, "_about": {"element": "msg_box_42", "role": "Edit", "app": "chrome.exe"}})
    return out


def test_every_action_tools_words_have_no_digits_ids_commands_text_or_addresses() -> None:
    from yuki.agent.tools import ACTION_TOOL_NAMES
    from yuki.ui.step_words import describe_step

    for name in ACTION_TOOL_NAMES:
        for args in _inputs_for(name):
            for done in (False, True):
                line = describe_step(name, args, done=done)
                assert line and line[0].isupper(), (name, line)
                assert not any(ch.isdigit() for ch in line), (name, args, line)
                assert "_" not in line and "://" not in line and "+" not in line, (name, line)
                for leak in ("Get-Process", "Ana", "example", "ctrl", "enter", "Tools", "run_me", "msg", "Inbox"):
                    assert leak.casefold() not in line.casefold(), (name, leak, line)
                if "_" in name:   # never the tool's name ("run powershell", "launch app")
                    assert name.replace("_", " ") not in line.casefold(), (name, line)


def test_the_words_are_verbal_and_use_the_brains_what_and_the_controls_name() -> None:
    from yuki.ui.step_words import describe_step

    about = {"_about": {"element": "Done", "role": "Button", "app": "Discord"}}
    assert describe_step("click", {"element": 25, **about}) == "Clicking Done"
    assert describe_step("click", {"element": 25, **about}, done=True) == "Clicked Done"
    assert describe_step("hotkey", {"keys": ["ctrl", "l"], "what": "open the address bar"}) == "Opening the address bar"
    assert describe_step("hotkey", {"keys": ["ctrl", "l"]}) == "Pressing a shortcut"
    assert describe_step("run_powershell", {"command": "x", "what": "close Slack"}) == "Closing Slack"
    assert describe_step("run_powershell", {"command": "x", "what": "close Slack"}, done=True) == "Closed Slack"
    assert describe_step("run_powershell", {"command": "x", "what": "stop the timer"}, done=True) == "Stopped the timer"
    assert describe_step("run_powershell", {"command": "x", "what": "quit Slack fully"}) == "Quitting Slack fully"
    assert describe_step("run_powershell", {"command": "Stop-Process 4412"}) == "Working on your PC"
    assert describe_step("window_action", {"hwnd": 9, "action": "close", "_about": {"app": "Slack"}}) == "Closing Slack"
    assert describe_step("focus_window", {"hwnd": 9, "_about": {"app": "Discord"}}) == "Switching to Discord"
    assert describe_step("launch_app", {"query": "spotify"}) == "Opening Spotify"
    assert describe_step("open_url", {"url": "https://x.example", "app": "Arc"}, done=True) == "Opened a page in Arc"
    assert describe_step("scroll", {"dy": 3, "element": 4}) == "Scrolling up the page"
    assert describe_step("type_text", {"text": "x", "_about": {"element": "Search", "role": "Edit"}}) == \
        "Typing in the Search box"
    # a what with a number or an address is not plain words: the step's own words instead
    assert describe_step("press", {"key": "volumeup", "what": "turn it up 3 notches"}) == "Pressing a key"


def test_the_ledger_reads_the_same_words_from_a_kept_step() -> None:
    import json

    from yuki.ui.web_data import step_what

    step = {"tool": "run_powershell", "app_name": "Windows",
            "input": json.dumps({"command": "Get-Process slack | Stop-Process", "what": "close Slack"})}
    assert step_what(step) == "Closed Slack"
    typed = {"tool": "type_text", "app_name": "Discord", "input": json.dumps({"text": "hey Ana", "press_enter": True})}
    assert step_what(typed) == "Typed and sent a message"
    clicked = {"tool": "click", "app_name": "Discord", "target": 'Button "Message", window "Ana - Discord"',
               "input": json.dumps({"role": "Button", "name": "Message"})}
    assert step_what(clicked) == "Clicked Message"
    assert step_what({"tool": "todo", "input": json.dumps({"action": "add", "text": "buy milk"})}) == \
        "Added to your list"


def test_no_ui_module_reads_the_command_typed_text_or_address_from_a_tool_input() -> None:
    """No run surface has a path that reads ``command``, ``text`` or ``url`` from a tool call's input."""
    names = {"tool_input", "args", "event_input", "inp", "acting"}
    # matching, never shown: the ledger finds the to-do a step added by its words, for its Undo
    allowed = {("web_data.py", "_todo_for")}
    bad: list[str] = []
    for path in sorted((ROOT / "yuki" / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(fn):
                    owners.setdefault(id(inner), fn.name)
        for node in ast.walk(tree):
            if (path.name, owners.get(id(node), "")) in allowed:
                continue
            key = owner = None
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                key, owner = node.slice.value, node.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                  and node.args and isinstance(node.args[0], ast.Constant)):
                key, owner = node.args[0].value, node.func.value
            if key in ("command", "text", "url") and (
                    (isinstance(owner, ast.Name) and owner.id in names)
                    or (isinstance(owner, ast.Attribute) and owner.attr == "input")):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert bad == []
    # the label function itself never names those fields at all
    words = (ROOT / "yuki" / "ui" / "step_words.py").read_text(encoding="utf-8")
    body = words.split('"""', 2)[2].split("# -- the return card")[0]   # the label functions
    for field in ('"command"', '"text"', '"url"', '"keys"', '"key"'):
        assert field not in body, field
    from yuki.ui.step_words import LABEL_KEYS

    assert not {"command", "text", "url", "keys", "key"} & set(LABEL_KEYS)


def test_what_is_on_the_tools_and_a_note_wherever_calls_are_compared() -> None:
    from yuki.agent.loopguard import NOTE_FIELDS, signature
    from yuki.agent.tools import STEP_ABOUT_FIELD, TOOLS_BY_NAME, WHAT_FIELD
    from yuki.ui.step_words import ABOUT, WHAT

    assert (WHAT_FIELD, STEP_ABOUT_FIELD) == (WHAT, ABOUT)
    assert TOOLS_BY_NAME["run_powershell"]["input_schema"]["required"] == ["command", "what", "risk"]
    for name in ("type_text", "hotkey", "press"):
        schema = TOOLS_BY_NAME[name]["input_schema"]
        assert "what" in schema["properties"] and "what" not in schema.get("required", [])
    assert {"what", "_about"} <= NOTE_FIELDS
    assert signature({"keys": ["ctrl", "l"], "what": "open the address bar"}) == signature({"keys": ["ctrl", "l"]})


def test_only_a_certain_send_counts_as_one() -> None:
    from yuki.agent.tools import call_traits, sends_for_certain

    assert sends_for_certain(call_traits("type_text", {"text": "hi", "press_enter": True}))
    assert sends_for_certain(call_traits("type_text", {"text": "hi\n"}))
    assert sends_for_certain(call_traits("click", {"element": 3}, typed_pending=True))
    assert not sends_for_certain(call_traits("type_text", {"text": "hi"}))
    assert not sends_for_certain(call_traits("hotkey", {"keys": ["ctrl", "l"]}))      # a key only may send
    assert not sends_for_certain(call_traits("click", {"element": 3}, role="Button"))


def test_the_agent_puts_the_targets_names_and_the_send_on_the_call_event(tmp_path: Path) -> None:
    from test_d_brain import CHROME, Client, _agent, _block, _response, _run

    from yuki.log.events import ToolCall, ToolResult

    client = Client([
        _response(_block("tool_use", id="t1", name="type_text",
                         input={"text": "hey Ana", "press_enter": True, "expect_hwnd": CHROME, "what": "send Ana hi",
                                "risk": {"reversible": False, "user_asked": True, "effect": "Sends Ana a message"}})),
        _response(_block("tool_use", id="t2", name="done", input={"message": "Sent.", "about": []})),
    ])
    events = _run(_agent(client, tmp_path, judge=False), "say hi to Ana")
    call = next(e for e in events if isinstance(e, ToolCall) and e.name == "type_text")
    about = call.input["_about"]
    assert about["sends"] is True and about["effect"] == "Sends Ana a message"
    assert not any(isinstance(v, int) and not isinstance(v, bool) for v in about.values())   # no window numbers
    result = next(e for e in events if isinstance(e, ToolResult) and e.name == "type_text")
    assert result.about["what"] == "send Ana hi" and result.about["_about"]["sends"] is True


# -- the stop: who asked, and the card -----------------------------------------------------------------------------


def test_every_plain_cancel_is_a_stop_with_who_asked() -> None:
    from yuki.agent.loop import CANCELLED, WITHDRAWN
    from yuki.ui.runtime import AgentRuntime

    emitted: list[tuple] = []
    lane = lambda how: SimpleNamespace(cancel_job=lambda rid, reason: how)   # noqa: E731
    fake = SimpleNamespace(worker=lane("running"), front_desk=lane("unknown"),
                           ui_log=SimpleNamespace(event=lambda *a, **k: None),
                           cancelled=SimpleNamespace(emit=lambda *a: emitted.append(a)))
    assert AgentRuntime.cancel(fake, 4) == "running"
    assert AgentRuntime.cancel(fake, 5, CANCELLED, by="strip") == "running"
    AgentRuntime.cancel(fake, 6, WITHDRAWN)                     # replaced by a newer request: not a stop
    assert emitted == [(4, ""), (5, "strip")]


def _bare_ui(qapp: Any, *, voice_owns: bool) -> Any:
    from PySide6.QtCore import QObject

    from yuki.ui.app import YukiUi
    from yuki.ui.status import StatusStrip

    ui = YukiUi.__new__(YukiUi)
    QObject.__init__(ui)
    events: list[tuple] = []
    ui.ui_log = SimpleNamespace(event=lambda name, **f: events.append((name, f)))
    ui.events = events
    ui.strip = StatusStrip()
    card = SimpleNamespace(set_body=lambda *a, **k: None, set_meta=lambda *a: None)
    ui.overlay = SimpleNamespace(isVisible=lambda: False, closing=False, dismiss=lambda: None,
                                 add_card=lambda *a, **k: card)
    ui.runtime = SimpleNamespace(memory=None, cancel=lambda rid, reason="cancelled": "running")
    ui.nudges = SimpleNamespace(make_way=lambda: None, try_show=lambda: None)
    ui.voice = SimpleNamespace(owns=lambda rid: voice_owns, interrupt=lambda: None)
    ui.bindings = {"cancel": "alt+shift+x"}
    ui.window_host = SimpleNamespace(open_window=lambda *a: None)
    ui.island = SimpleNamespace(inputs=SimpleNamespace(fullscreen=False), lesson_available=False)
    ui._island_answered = lambda rid: None
    ui.cards, ui._settled, ui._acting = {}, {}, set()
    ui._question = None
    ui._runs, ui._stopped, ui._stop_hint_shown = {}, {}, False
    ui._ask_runs = {}
    ui.return_card = None
    return ui


# The 13:28 run of 3 Oct (revamp 38 §8b), with invented names: open the chat app, open a page, click around, type the
# message with Enter (declared irreversible); the voice cancels 0.6 s later; the send comes back "Not sent".
RUN_1328 = [
    ("launch_app", {"query": "discord"}, True),
    ("open_url", {"url": "https://www.youtube.com", "app": "Arc", "_about": {"app": "Arc"}}, True),
    ("click", {"element": 25, "expect_hwnd": 264022, "_about": {"role": "Button", "element": "Ana", "app": "Discord"}},
     True),
    ("look_at_window", {"hwnd": 264022}, False),
    ("look_at_window", {"hwnd": 264022}, True),
    ("click", {"element": 31, "expect_hwnd": 264022, "_about": {"app": "Discord"}}, False),
    ("focus_window", {"hwnd": 133596, "_about": {"app": "Discord"}}, True),
    ("click", {"element": 40, "_about": {"role": "Edit", "element": "Message @Ana", "app": "Discord"}}, True),
    ("type_text", {"text": "hey Ana, are you on tonight?", "press_enter": True,
                   "risk": {"reversible": False, "user_asked": True, "effect": "Sends Ana a message"},
                   "_about": {"role": "Edit", "element": "Message @Ana", "app": "Discord", "sends": True,
                              "effect": "Sends Ana a message"}}, None),
]


def _all_texts(widget: Any) -> list[str]:
    out = []
    for w in [widget, *widget.findChildren(object)]:
        for attr in ("text", "_text", "label"):
            v = getattr(w, attr, None)
            try:
                v = v() if callable(v) else v
            except TypeError:
                continue
            if isinstance(v, str) and v:
                out.append(v)
    return out


def test_the_1328_stop_by_voice_reads_stopped_nothing_was_sent_with_no_undo_anywhere(qapp: Any) -> None:
    ui = _bare_ui(qapp, voice_owns=True)
    ui._on_started("worker", 4, "message Ana on Discord, then go to YouTube")
    for name, args, ok in RUN_1328:
        ui._on_tool_called("worker", 4, name, args)
        if ok is not None:
            ui._on_tool_finished("worker", 4, name, ok, "")
    ui._on_cancelled(4, "")                       # the voice's cancel (runtime.cancel(job) with no `by`)
    ui._on_tool_finished("worker", 4, "type_text", False, "Not sent: cancelled; nothing was typed or clicked")
    ui._on_failed("worker", 4, "Cancelled by the user.")
    settle = [f for n, f in ui.events if n == "settle"][-1]
    assert settle["tone"] == "stopped" and settle["by"] == "voice"
    assert ui.strip.label._full == "Stopped."
    pending = ui._return_pending
    view = pending["view"]
    assert (view["icon"], view["head"]) == ("stop", "Stopped")
    assert view["title"] == "Stopped before it finished. Nothing was sent."
    assert [r["text"] for r in view["rows"]] == ["Opened Discord and a page in Arc"]   # navigation, one line
    ui._on_return_ready({"id": 4, "learned": None})
    card = ui.return_card
    assert card.head.text().startswith("Stopped · ") and card.head.text().endswith(("AM", "PM"))
    assert card.title.text() == "Stopped before it finished. Nothing was sent."
    texts = " | ".join(_all_texts(card))
    assert "undo" not in texts.casefold(), texts
    assert not any(ch.isdigit() for ch in " ".join(r["text"] for r in view["rows"]))
    for leak in ("element", "hwnd", "expect", "Pressing", "youtube.com", "hey Ana"):
        assert leak not in texts, (leak, texts)
    from yuki.ui import lattice as L

    keys = [k for k in card.findChildren(L.Key) if not k.isHidden()]
    assert keys and all("undo" not in k.text().casefold() for k in keys)
    assert card.took.text().endswith("s")         # the footer: just the time ("41 s"), and Why
    card.close()


def test_the_return_card_by_outcome() -> None:
    from yuki.ui.step_words import return_view

    closed = [{"name": "run_powershell", "ok": True,
               "input": {"what": "close Slack", "_about": {"changes": True}}}]
    done = return_view("done", "Slack is closed, background too.", closed)
    assert (done["head"], done["title"]) == ("Done", "Slack is closed, background too.")
    assert done["rows"] == [{"text": "Closed Slack", "icon": "check"}]
    failed = return_view("failed", "I couldn't find that window.",
                         [{"name": "run_powershell", "ok": False, "input": {"what": "close Slack",
                                                                            "_about": {"changes": True}}}])
    assert failed["head"] == "Didn’t finish" and failed["rows"] == [{"text": "Didn’t close Slack", "icon": "dash"}]
    many = [{"name": "run_powershell", "ok": True, "input": {"what": f"close {app}", "_about": {"changes": True}}}
            for app in ("Slack", "Teams", "Zoom", "Steam", "Spotify", "Notion")]
    six = return_view("done", "Closed them.", many)
    assert len(six["rows"]) == 4 and six["more"] == 2
    clicks_only = return_view("done", "Done.", [{"name": "click", "ok": True, "input": {"_about": {"element": "OK"}}}])
    assert not clicks_only["worth"]               # nothing to tell beyond the strip's answer: no card
    handed = return_view("handed", "Handed back. You have the controls.", closed)
    assert handed["head"] == "Handed back" and handed["icon"] == "hand"


def test_the_return_card_widget_has_no_undo_or_cant_undo_text(qapp: Any) -> None:
    from yuki.ui import lattice as L
    from yuki.ui.lattice_cards import ReturnCard

    card = ReturnCard()
    card.show_run(icon="check", head="Done", title="Closed Slack.", at="9:12 PM",
                  rows=[{"text": "Closed Slack", "icon": "check"}], more=0, took="4 s",
                  learned={"id": "pb:1", "text": "Close Slack from the tray first."})
    card.learn._decide("save")                    # the Learned box's confirmation: no Undo key either
    texts = " | ".join(_all_texts(card)).casefold()
    assert "undo" not in texts and "can’t" not in texts and "can't" not in texts
    assert all("undo" not in k.text().casefold() for k in card.findChildren(L.Key))
    card.close()


@pytest.fixture
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
