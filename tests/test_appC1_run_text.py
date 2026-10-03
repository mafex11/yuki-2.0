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
    ui.runtime = SimpleNamespace(memory=None, cancel=lambda rid, reason="cancelled", by="": "running")
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
    # the title is what it achieved, never the reply; a row that only repeats the title is left out
    assert (done["head"], done["title"], done["rows"], done["worth"]) == ("Done", "Closed Slack", [], True)
    failed = return_view("failed", "I couldn't find that window.",
                         [{"name": "run_powershell", "ok": False, "input": {"what": "close Slack",
                                                                            "_about": {"changes": True}}}])
    assert failed["head"] == "Didn’t finish" and failed["title"] == "I couldn’t finish that."
    assert failed["rows"] == [{"text": "Didn’t finish closing Slack", "icon": "dash"}]
    many = [{"name": "run_powershell", "ok": True, "input": {"what": f"close {app}", "_about": {"changes": True}}}
            for app in ("Slack", "Teams", "Zoom", "Steam", "Spotify", "Notion")]
    six = return_view("done", "Closed them.", many)
    assert six["title"] == "Closed Notion" and len(six["rows"]) == 4 and six["more"] == 1   # the title's row left out
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


# -- review fixes (report 46 "Review fixes") ------------------------------------------------------------------------


def test_f1_the_title_is_what_was_achieved_never_just_done() -> None:
    from yuki.agent.tools import ACHIEVED_FIELD, TOOLS_BY_NAME
    from yuki.ui.step_words import ACHIEVED, LABEL_KEYS, achievement, return_view

    assert ACHIEVED_FIELD == ACHIEVED and ACHIEVED in LABEL_KEYS
    # vfix2 (report 47, N3) adds "states": each state the message tells, with the step that showed it
    assert TOOLS_BY_NAME["done"]["input_schema"]["required"] == ["message", "about", "achieved", "states"]
    closed = {"name": "run_powershell", "ok": True, "input": {"what": "close Slack", "_about": {"changes": True}}}
    # the reply is "Done." and the brain said nothing better: the step's own done words
    assert return_view("done", "Done.", [closed])["title"] == "Closed Slack"
    done_call = lambda line: {"name": "done", "ok": True, "input": {"achieved": line}}   # noqa: E731
    assert achievement([closed, done_call("Closed Slack and its tray icon")]) == "Closed Slack and its tray icon"
    for weak in ("Done", "done!", "Sent", "", "ran Stop-Process -Name slack", "C:\\x\\y did it"):
        assert achievement([closed, done_call(weak)]) == "Closed Slack", weak
    # a volume-only run (the brief's own example) is a result: it gets a card, titled by the brain's line
    volume = [{"name": "app_volume", "ok": True, "input": {"app": "spotify", "level": 60}},
              done_call("Turned Spotify up to 60%")]
    view = return_view("done", "Spotify's at 60 now.", volume)
    assert view["worth"] and view["title"] == "Turned Spotify up to 60%"
    assert return_view("done", "x", volume[:1])["title"] == "Set Spotify’s volume"
    closed_window = {"name": "window_action", "ok": True, "input": {"action": "close", "_about": {"app": "Slack"}}}
    assert return_view("done", "Done", [closed_window])["title"] == "Closed Slack"
    switched = {"name": "window_action", "ok": True, "input": {"action": "focus", "_about": {"app": "Slack"}}}
    assert not return_view("done", "Done", [switched])["worth"]          # switching is getting there, not a result
    assert achievement([switched]) == ""                                 # nothing said it: the caller keeps the reply


def test_f1_f2_the_strip_and_the_thread_end_on_the_achievement_or_a_plain_reason(qapp: Any) -> None:
    from yuki.ui.chat_model import ChatModel

    ui = _bare_ui(qapp, voice_owns=False)
    model = ChatModel()
    ui.window_host = SimpleNamespace(open_window=lambda *a: None, emit=model.apply)
    model.apply({"type": "user", "job": 6, "text": "message Ana that I'm late", "at": 1.0})
    model.apply({"type": "started", "job": 6, "lane": "worker", "at": 1.1})
    ui._on_started("worker", 6, "message Ana that I'm late")
    send = {"text": "running late, sorry", "press_enter": True, "what": "send Ana the message",
            "_about": {"sends": True, "app": "Discord", "role": "Edit", "element": "Message @Ana"}}
    ui._on_tool_called("worker", 6, "type_text", send)
    ui._on_tool_finished("worker", 6, "type_text", True, "typed 19 chars, Enter pressed")
    ui._on_tool_called("worker", 6, "done", {"message": "done!", "about": [], "achieved": "Messaged Ana on Discord"})
    ui._on_finished("worker", 6, "done!")
    model.apply({"type": "finished", "job": 6, "text": "done!", "at": 3.0})
    assert ui.strip.label._full == "Messaged Ana on Discord"
    assert ui._return_pending["view"]["title"] == "Messaged Ana on Discord"
    last = model.items[-1].steps[-1]
    assert (last.kind, last.label) == ("say", "Messaged Ana on Discord")     # the thread's final line, the same words
    assert model.items[-1].text == "done!"                                    # the reply itself stays the reply
    # a run that acted and failed: a plain reason on the strip and the card, the loop's text only in the log
    ui._on_started("worker", 8, "close Slack")
    ui._on_tool_called("worker", 8, "run_powershell", {"command": "x", "what": "close Slack",
                                                        "_about": {"changes": True}})
    ui._on_tool_finished("worker", 8, "run_powershell", False, "powershell exit 1 in 312 ms")
    raw = "The model stopped with nothing to say (stop_reason=end_turn)."
    ui._on_failed("worker", 8, raw)
    assert ui.strip.label._full == "I couldn’t finish that."
    assert ui._return_pending["view"]["title"] == "I couldn’t finish that."
    settle = [f for n, f in ui.events if n == "settle"][-1]
    assert settle["text"] == raw and settle["shown"] == "I couldn’t finish that."


def test_f3_f4_thread_steps_show_plain_words_never_summaries_or_hand_arguments() -> None:
    import json

    from yuki.ui.chat_model import ChatModel
    from yuki.ui.step_words import describe_hand
    from yuki.ui.web_data import step_what
    from yuki.voice.hands import hand_words

    m = ChatModel()
    m.apply({"type": "user", "job": 3, "text": "close slack", "at": 1.0})
    m.apply({"type": "tool_called", "job": 3, "name": "run_powershell", "label": "Closing Slack", "at": 1.2})
    m.apply({"type": "tool_finished", "job": 3, "name": "run_powershell", "ok": True, "at": 1.5,
             "summary": "powershell exit 0 in 312 ms; 1 line: Stop-Process : Cannot find a process"})
    m.apply({"type": "tool_called", "job": 3, "name": "click", "label": "Clicking Quit", "at": 1.6})
    m.apply({"type": "tool_finished", "job": 3, "name": "click", "ok": False, "summary": "ToolError: hwnd 264022",
             "at": 1.7})
    steps = m.items[-1].steps
    assert [(s.label, s.result) for s in steps] == [("Closing Slack", ""), ("Clicking Quit", "Didn’t work")]
    # the voice's hands: the same words as the thinker's, never the tool name or its arguments
    assert hand_words("app_volume", {"app": "spotify", "change": -20}) == "Turned Spotify down"
    assert hand_words("window", {"window": 264022, "action": "close"}, "Slack.exe") == "Closed Slack"
    assert hand_words("open_url", {"url": "https://www.example.com/x"}) == "Opened a page"
    assert hand_words("media_key", {"key": "nexttrack"}) == "Skipped to the next track"
    assert describe_hand("scroll", {"direction": "up", "window": 5}) == "Scrolled up the page"
    m.apply({"type": "voice_user", "text": "turn spotify down", "final": True, "turn": "v-1", "at": 2.0})
    m.apply({"type": "voice_hand", "turn": "v-1", "call": "v-1:1", "name": "app_volume", "phase": "end", "ok": True,
             "label": "Turned Spotify down", "result": "spotify.exe 60 -> 40", "ms": 80, "at": 2.1})
    hand = m.items[-1].steps[-1]
    assert (hand.label, hand.result) == ("Turned Spotify down", "")
    # the ledger's hand traces (the hand's own tool names) read the same way, never "Did a step"
    kept = lambda tool, args, app=None: step_what({"tool": tool, "input": json.dumps(args), "app_name": app})  # noqa
    assert kept("window", {"action": "close"}, "Slack") == "Closed Slack"
    assert kept("open_app", {"name": "spotify"}) == "Opened Spotify"
    assert kept("media_key", {"key": "playpause", "want": "paused"}) == "Paused the music"
    assert kept("type", {"text": "hey Ana"}, "Discord") == "Typed in Discord"


def test_f3_the_window_host_sends_plain_hand_and_helper_words(qapp: Any) -> None:
    from PySide6.QtCore import QObject

    from yuki.ui.window_host import WindowHost

    host = WindowHost.__new__(WindowHost)
    QObject.__init__(host)
    sent: list[dict] = []
    host.emit = sent.append
    host._hands = 0
    host._on_voice_event({"type": "hand_step", "turn_id": "v-2", "name": "app_volume", "args": "", "ok": True,
                          "result": "spotify.exe level 40", "ms": 50})
    assert sent[-1]["label"] == "Checked which apps play sound" and "result" not in sent[-1]
    host._on_helper_step("worker", 4, 1, "tool", "read_page: 3 unread, page https://x.example", True)
    assert "read_page" not in sent[-1]["text"] and "example" not in sent[-1]["text"]


def test_f5_the_ledger_says_results_in_plain_words_with_no_undo_talk() -> None:
    from yuki.ui.web_data import run_rows, step_result

    assert step_result({"ok": False, "result": "Not sent: cancelled; nothing was typed or clicked"}) == \
        ("didn’t work", "failed")
    assert step_result({"ok": False, "result": "x"}, stopped=True) == ("stopped before it finished", "stopped")
    assert step_result({"ok": True, "verdict": True, "ms": 412}) == ("done, checked on screen", "verified")
    run = {"key": "k", "words": "close slack", "outcome": "cancelled", "at": 100.0,
           "steps": [{"step": 1, "tool": "click", "kind": "action", "ok": True, "verdict": True, "ms": 412, "at": 100.0,
                      "input": "{}", "app_name": "Slack"},
                     {"step": 2, "tool": "type_text", "kind": "action", "ok": False, "at": 101.0, "input": "{}",
                      "result": "Not sent: cancelled; nothing was typed or clicked"}]}
    rows = run_rows(run, [], 200.0)
    assert [r["result"] for r in rows] == ["done, checked on screen", "stopped before it finished"]
    assert all("cant_undo" not in r for r in rows) and rows[1]["f"] == "did"
    js = (ROOT / "yuki" / "ui" / "web" / "pages" / "activity.js").read_text(encoding="utf-8")
    assert "can’t undo'" not in js and "cant_undo" not in js


def test_f6_the_brains_what_in_any_tense_and_never_a_command() -> None:
    from yuki.ui.step_words import describe_step, return_view

    say = lambda what, done=False: describe_step("run_powershell", {"command": "x", "what": what}, done=done)  # noqa
    assert (say("closed Slack"), say("closed Slack", True)) == ("Closing Slack", "Closed Slack")
    assert (say("closing Slack"), say("closing Slack", True)) == ("Closing Slack", "Closed Slack")
    assert (say("sends Ana a message"), say("sends Ana a message", True)) == ("Sending Ana a message",
                                                                              "Sent Ana a message")
    assert say("stopped the timer", True) == "Stopped the timer" and say("added milk", True) == "Added milk"
    assert say("called Ana") == "Calling Ana" and say("messaged Ana on Discord") == "Messaging Ana on Discord"
    for code in ("run taskkill /im slack.exe /f", "Get-Process slack | Stop-Process -Force", "Stop-Process slack",
                 "kill $proc", "set volume=40", "close Slack -Force", "open notes.txt"):
        assert say(code) == "Working on your PC", code
    failed = return_view("failed", "x", [{"name": "run_powershell", "ok": False,
                                          "input": {"what": "closing Slack", "_about": {"changes": True}}}])
    assert failed["rows"][0]["text"] == "Didn’t finish closing Slack"


def test_f7_reading_the_list_is_not_a_change() -> None:
    from yuki.ui.step_words import describe_step, is_effect, return_view

    assert not is_effect("todo", {"action": "list"}) and is_effect("todo", {"action": "add"})
    assert describe_step("todo", {"action": "list"}, done=True) == "Checked your list"
    view = return_view("done", "x", [{"name": "todo", "ok": True, "input": {"action": "list"}}])
    assert view["rows"] == [] and not view["worth"]


def test_f8_every_stop_says_who_asked() -> None:
    from yuki.ui.app import YukiUi
    from yuki.voice.bridge import VoiceBridge

    calls: list[tuple] = []
    ui = SimpleNamespace(_stopped={}, voice=SimpleNamespace(owns=lambda rid: False, interrupt=lambda: None),
                         runtime=SimpleNamespace(cancel=lambda rid, by="": calls.append((rid, by)) or "running"),
                         ui_log=SimpleNamespace(event=lambda *a, **k: None), _stop_keep_scrolling=lambda via: None)
    YukiUi._on_strip_stop(ui, 5)
    assert calls == [(5, "strip")] and ui._stopped == {5: "strip"}
    job = SimpleNamespace(cancel_requested=False, log=None, job_id=9)
    bridge = SimpleNamespace(runtime=SimpleNamespace(cancel=lambda rid, by="": calls.append((rid, by)) or "running"),
                             _log=lambda *a, **k: None)
    VoiceBridge._cancel(bridge, job)
    assert calls[-1] == (9, "voice")
    old = SimpleNamespace(runtime=SimpleNamespace(cancel=lambda rid: calls.append((rid, "?")) or "running"),
                          _log=lambda *a, **k: None)
    VoiceBridge._cancel(old, job)                    # a runtime without `by` (older fakes) still cancels
    assert calls[-1] == (9, "?")


def test_f9_the_ledger_names_the_todo_and_rule_it_kept_from_memorys_record() -> None:
    from yuki.ui.web_data import run_rows

    run = {"key": "k", "words": "remember to water the basil", "outcome": "final", "at": 1000.0,
           "steps": [{"step": 1, "tool": "todo", "kind": "memory", "ok": True, "at": 1000.0,
                      "input": '{"action": "add", "text": "Water the basil"}'},
                     {"step": 2, "tool": "remember_rule", "kind": "memory", "ok": True, "at": 1001.0,
                      "input": '{"text": "never call after ten"}'}]}
    todos = [{"id": "todo:3", "text": "Water the basil", "source": "user", "status": "open", "since": 1000.0}]
    rules = [{"ref": "rule:4", "text": "No calls after 10 PM", "at": 1002.0},
             {"ref": "rule:1", "text": "An old rule", "at": 10.0}]
    rows = run_rows(run, todos, 1010.0, rules)
    assert [r["what"] for r in rows] == ["Added to your list “Water the basil”", "Kept a rule “No calls after 10 PM”"]
    assert [r["what"] for r in run_rows(run, [], 1010.0)] == ["Added to your list", "Kept a rule"]   # no record


@pytest.fixture
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
