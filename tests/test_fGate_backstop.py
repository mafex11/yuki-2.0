"""fGate (D17): nothing ever reaches a game in front. The input layer's backstop refuses every key, click, pointer move,
focus (UI Automation focus too), window restore/show/activation, opened link and app launch while a game is in front
(or aimed at a game's window), logs ``input_refused_game``, and a source-walking test fails on any such call left
unguarded in ``yuki/actions``, ``yuki/perception``, ``yuki/skills`` or ``yuki/connect``.

Offline: "a game in front" is a stub; every Win32 call that would act (SendInput, SetCursorPos, ShowWindow,
SetForegroundWindow, os.startfile, PowerShell) is replaced by a recorder, and nothing reads the live desktop.
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path
from typing import Any

import pytest

from yuki.memory import gamegate as G

ROOT = Path(__file__).resolve().parents[1] / "yuki"
GAME = "valorant-win64-shipping"


@pytest.fixture
def game(monkeypatch):
    """A stub of the game facts: ``state["front"]`` (the game in front, "" none), ``windows`` (hwnd -> game),
    ``pids`` (pid -> game), ``points`` ((x, y) -> hwnd). Refusals are recorded in ``state["log"]``."""
    state: dict[str, Any] = {"front": GAME, "windows": {}, "pids": {}, "points": {}, "log": []}
    monkeypatch.setattr(G, "game_in_front", lambda: state["front"])
    monkeypatch.setattr(G, "window_game", lambda hwnd: state["windows"].get(int(hwnd), ""))
    monkeypatch.setattr(G, "pid_game", lambda pid, front=False, games=None: state["pids"].get(int(pid), ""))
    monkeypatch.setattr(G, "_point_root", lambda x, y: state["points"].get((x, y), 0))
    monkeypatch.setattr(G, "_window_pid", lambda hwnd: 1000 + int(hwnd) if hwnd else 0)
    monkeypatch.setattr(G, "_foreground_root", lambda: 1)

    def listen(type: str, **fields: Any) -> None:
        state["log"].append((type, fields))

    G.add_refusal_listener(listen)
    yield state
    G.remove_refusal_listener(listen)


class _Recorder:
    """Stands in for a Win32 DLL / module: the acting calls are recorded and answered; anything else is refused."""

    def __init__(self, **answers: Any) -> None:
        self.calls: list[tuple] = []
        self._answers = answers

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any) -> Any:
            self.calls.append((name, *args))
            answer = self._answers.get(name, 1)
            return answer(*args) if callable(answer) else answer

        return call

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# -- the rule --------------------------------------------------------------------------------------------------------


def test_refuse_input_front_target_point_and_front_ok(game) -> None:
    assert "valorant-win64-shipping" in G.refuse_input("keys") and "in front" in G.refuse_input("keys")
    assert game["log"][-1] == ("input_refused_game", {"action": "keys", "game": GAME, "where": "front"})
    assert G.refuse_input("scroll", front_ok=True) is None                         # a pattern call: allowed
    game["front"] = ""
    assert G.refuse_input("keys") is None
    game["windows"][77] = GAME
    assert "belongs to a game" in G.refuse_input("focus", hwnd=77)                 # aimed at a background game
    game["pids"][4242] = GAME
    assert G.refuse_input("uia_focus", pid=4242) is not None
    game["points"][(10, 20)] = 5
    game["pids"][1005] = GAME
    assert G.refuse_input("mouse", point=(10, 20)) is not None                     # the game window under the pointer
    assert game["log"][-1][1]["where"] == "point"
    assert G.refuse_input("mouse", point=(30, 40)) is None


def test_refuse_input_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(G, "game_in_front", lambda: (_ for _ in ()).throw(RuntimeError("broken read")))
    assert "could not check" in G.refuse_input("keys")


def test_the_local_read_of_the_game_in_front(monkeypatch) -> None:
    """D9 read locally: the foreground window's program by judge_program (the fixed list), Direct3D exclusive full
    screen for the program in front only, Yuki's own process never; the verdict is reused while the window stays."""
    import os

    reads: list[int] = []
    programs = {4100: (r"VALORANT-Win64-Shipping.exe", ""), 4200: ("Discord.exe", ""), os.getpid(): ("python.exe", "")}
    monkeypatch.setattr(G, "_program", lambda pid: reads.append(pid) or programs.get(pid, ("", "")))
    monkeypatch.setattr(G, "_d3d_exclusive", lambda: False)
    monkeypatch.setattr(G, "_front_cache", None)
    fg = {"hwnd": 41, "pid": 4100}
    monkeypatch.setattr(G, "_foreground_root", lambda: fg["hwnd"])
    monkeypatch.setattr(G, "_window_pid", lambda hwnd: fg["pid"] if hwnd == fg["hwnd"] else 0)
    assert G.game_in_front() == "valorant-win64-shipping"
    assert G.game_in_front() == "valorant-win64-shipping" and reads == [4100]      # reused for FRONT_FRESH_S
    fg.update(hwnd=42, pid=4200)
    assert G.game_in_front() == ""                                                  # another window: read again
    monkeypatch.setattr(G, "_d3d_exclusive", lambda: True)
    monkeypatch.setattr(G, "_front_cache", None)
    assert G.game_in_front() == "discord"                                           # exclusive full screen in front
    assert G.pid_game(4200) == ""                                                   # ...but only for the front
    assert G.pid_game(os.getpid(), front=True) == ""                                # Yuki itself: never
    monkeypatch.setattr(G, "_d3d_exclusive", lambda: False)
    monkeypatch.setattr(G, "_front_cache", None)
    G.set_front_source(lambda: "skyrunner")                                         # memory's Now board adds one
    try:
        assert G.game_in_front() == "skyrunner"
    finally:
        G.set_front_source(None)


# -- the input layer ---------------------------------------------------------------------------------------------------


@pytest.fixture
def user32(monkeypatch):
    import yuki.actions.input as inp

    fake = _Recorder(GetForegroundWindow=1, SendInput=lambda n, ptr, size: n)
    real = inp._user32

    class Proxy:
        def __getattr__(self, name: str) -> Any:
            if name in ("SendInput", "SetCursorPos", "GetForegroundWindow"):
                return getattr(fake, name)
            return getattr(real, name)

    monkeypatch.setattr(inp, "_user32", Proxy())
    monkeypatch.setattr(inp, "cursor_position", lambda: (500, 500))
    return fake


def test_keys_pointer_and_text_send_nothing_with_a_game_in_front(game, user32) -> None:
    import yuki.actions.input as inp

    assert inp._send_each(inp._named_key_records("a", up=False)) == 0
    assert inp._send_mouse(0x0002) == 0
    assert inp._move(10, 10) is False
    sent, expected, refusal = inp._send_text("hello")
    assert (sent, expected) == (0, 0) and "in front" in refusal
    for result in (inp.press("enter"), inp.hotkey("ctrl", "t"), inp.type_text("hi"), inp.click(10, 10),
                   inp.point_at(10, 10)):
        assert result.ok is False and result.details["refused"] == "game" and "in front" in result.summary
    assert "SendInput" not in user32.names() and "SetCursorPos" not in user32.names()
    assert {f["action"] for t, f in game["log"] if t == "input_refused_game"} >= {"keys", "mouse", "pointer",
                                                                                  "type_text", "click"}


def test_without_a_game_the_primitives_still_send(game, user32) -> None:
    import yuki.actions.input as inp

    game["front"] = ""
    assert inp._send_each(inp._named_key_records("a", up=False)) >= 1
    assert inp._move(10, 10) is True
    assert user32.names().count("SetCursorPos") == 1 and "SendInput" in user32.names()


def test_a_click_over_a_background_game_window_is_refused_keys_are_not(game, user32) -> None:
    import yuki.actions.input as inp

    game["front"] = ""
    game["points"][(500, 500)] = 9
    game["pids"][1009] = GAME
    assert inp._send_mouse(0x0002) == 0                                            # the pointer is over the game
    assert inp._send_each(inp._named_key_records("a", up=False)) >= 1              # keys go to the window in front
    assert game["log"][-1][1]["where"] == "point"


def test_a_game_coming_to_the_front_mid_string_stops_the_typing(game, user32, monkeypatch) -> None:
    import yuki.actions.input as inp

    calls = {"n": 0}

    def front() -> str:
        calls["n"] += 1
        return GAME if calls["n"] > 3 else ""

    game["front"] = ""
    monkeypatch.setattr(G, "game_in_front", front)
    sent, expected, refusal = inp._send_text("abcdef")
    assert 0 < sent < 12 and "after" in refusal and "in front" in refusal


# -- windows, focus, links and launches --------------------------------------------------------------------------------


@pytest.fixture
def desk(monkeypatch):
    import yuki.actions.launch as launch

    gui = _Recorder(IsWindow=1, IsIconic=lambda h: launch_state["iconic"], IsWindowVisible=1, IsWindowEnabled=1)
    launch_state = {"iconic": False}

    def show(hwnd, cmd):
        launch_state["iconic"] = cmd in (6, 7, 2, 11)   # SW_MINIMIZE, SW_SHOWMINNOACTIVE, SW_SHOWMINIMIZED, SW_FORCEMIN

    gui._answers["ShowWindow"] = show
    user = _Recorder(GetForegroundWindow=99, IsZoomed=0, GetWindowThreadProcessId=0)
    kernel = _Recorder(GetCurrentThreadId=1)
    shell = _Recorder()
    started: list = []
    monkeypatch.setattr(launch, "win32gui", gui)
    monkeypatch.setattr(launch, "_user32", user)
    monkeypatch.setattr(launch, "_kernel32", kernel)
    monkeypatch.setattr(launch, "run_powershell", lambda *a, **k: shell.calls.append(("run_powershell", *a)))
    monkeypatch.setattr(launch.os, "startfile", lambda *a: started.append(a), raising=False)
    monkeypatch.setattr(launch, "get_start_apps", lambda *a, **k: shell.calls.append(("get_start_apps",)) or [])
    monkeypatch.setattr(launch, "window_info", lambda hwnd: None)
    import yuki.perception.windows as windows

    monkeypatch.setattr(windows, "window_state", lambda hwnd: "minimized" if launch_state["iconic"] else "normal")
    return launch, gui, user, shell, started


def _acted(gui: _Recorder, user: _Recorder) -> list[str]:
    acting = {"ShowWindow", "SetForegroundWindow", "BringWindowToTop", "SwitchToThisWindow", "AttachThreadInput",
              "PostMessage"}
    return [n for n in gui.names() + user.names() if n in acting]


def test_focus_restore_show_and_window_actions_are_refused_with_a_game_in_front(game, desk) -> None:
    launch, gui, user, shell, started = desk
    result = launch.focus_window(5)
    assert result.ok is False and result.details["refused"] == "game" and "in front" in result.summary
    assert launch._try_activate(5) is False
    assert launch._show_window(5) is False
    for action in ("restore", "maximize", "fullscreen", "close", "focus"):
        r = launch.window_action(5, action)
        assert r.ok is False and "game" in r.summary, action
    assert _acted(gui, user) == []
    logged = {f["action"] for t, f in game["log"] if t == "input_refused_game"}
    assert logged >= {"focus", "window_show", "window_restore", "window_maximize", "window_fullscreen",
                      "window_close"}


def test_minimising_another_window_stays_allowed_and_never_activates(game, desk) -> None:
    launch, gui, user, shell, started = desk
    r = launch.window_action(5, "minimize")
    assert r.ok is True
    assert [c for c in gui.calls if c[0] == "ShowWindow"] == [("ShowWindow", 5, 7)]   # SW_SHOWMINNOACTIVE
    game["windows"][6] = GAME
    assert launch.window_action(6, "minimize").ok is False                         # a game's own window: never


def test_a_game_coming_to_the_front_mid_focus_stops_the_next_try(game, desk, monkeypatch) -> None:
    launch, gui, user, shell, started = desk
    calls = {"n": 0}

    def front() -> str:
        calls["n"] += 1
        return GAME if calls["n"] > 1 else ""

    monkeypatch.setattr(G, "game_in_front", front)
    r = launch.focus_window(5, timeout_s=0.2)
    assert r.ok is False and r.details.get("refused") == "game" and _acted(gui, user) == []


def test_links_and_launches_are_refused_with_a_game_in_front(game, desk) -> None:
    launch, gui, user, shell, started = desk
    r = launch.open_url("spotify:playlist:0000000000000000000000")
    assert r.ok is False and r.details["refused"] == "game"
    r = launch.launch_app("Spotify", args=["--minimized"])
    assert r.ok is False and r.details["refused"] == "game" and r.details["launched"] is False
    assert launch._activate_packaged("Invented.App_0000!App", "", 1.0) == (None, launch._gamegate.refusal_text(
        GAME, "front"))
    assert launch._open_new_window({"appid": "Invented.App_0000!App"}, None, 1.0)[0] is None
    assert launch._start_entry("Invented.App_0000!App", 1.0)[0] is False
    assert started == [] and shell.calls == [] and _acted(gui, user) == []
    assert {f["action"] for t, f in game["log"] if t == "input_refused_game"} >= {"open_url", "launch"}


# -- the slider (critic H1) -------------------------------------------------------------------------------------------


class _Pattern:
    """A RangeValue that never takes a new value (a backgrounded CEF window ignoring SetValue)."""

    def __init__(self) -> None:
        self.CurrentValue, self.CurrentMinimum, self.CurrentMaximum = 0.3, 0.0, 1.0
        self.CurrentIsReadOnly, self.CurrentSmallChange = False, 0.01
        self.sets: list[float] = []

    def SetValue(self, value: float) -> None:
        self.sets.append(value)                     # reverts at once: the read stays 0.3


class _Element:
    CurrentProcessId = 4321

    def __init__(self) -> None:
        self.focus_calls = 0

    def SetFocus(self) -> None:
        self.focus_calls += 1


class _Tree:
    def __init__(self, slider: Any) -> None:
        from yuki.actions.slider import Node

        self.node = Node(role="Slider", name="Change volume", bounds=(0, 0, 100, 10))
        self._slider = slider

    def ranges(self, scope=None):
        return [self.node]

    def named(self, role, name):
        return []

    def parent(self, node):
        return None

    def slider(self, node):
        return self._slider

    def shown(self, node):
        return []


ELEMENT = {"role": "Slider", "name": "Change volume", "bounds": [0, 0, 100, 10], "center": [50, 5]}


def test_the_slider_rung_a_takes_no_focus_keys_or_pointer_with_a_game_in_front(game, user32) -> None:
    from yuki.actions.slider import _LiveSlider, run

    element, pattern = _Element(), _Pattern()
    hovers: list = []
    ok, summary, details = run(_Tree(_LiveSlider(element, pattern)), ELEMENT, to=80, press=None, hover=None,
                               hotkey=None, sleep=lambda s: None, settle_s=0.0, hold_s=0.0)
    assert ok is False and details["verified"] is False and pattern.sets            # tried RangeValue only
    assert element.focus_calls == 0 and user32.names() == [] and hovers == []
    assert "via" not in details or details["via"] != "keys"


def test_the_slider_with_a_press_has_its_focus_and_keys_refused(game, user32) -> None:
    import yuki.actions.input as inp
    from yuki.actions.slider import _LiveSlider, run

    element, pattern = _Element(), _Pattern()
    ok, _summary, details = run(_Tree(_LiveSlider(element, pattern)), ELEMENT, to=80,
                                press=lambda key, n: inp.press(key, times=n), hover=None, hotkey=None,
                                sleep=lambda s: None, settle_s=0.0, hold_s=0.0)
    assert ok is False and element.focus_calls == 0 and "focus_error" in details
    assert "SendInput" not in user32.names() and "SetCursorPos" not in user32.names()
    actions = {f["action"] for t, f in game["log"] if t == "input_refused_game"}
    assert {"uia_focus", "keys"} <= actions


def test_the_slider_focus_is_refused_on_a_game_element_even_behind(game) -> None:
    from yuki.actions.slider import _LiveSlider

    game["front"] = ""
    game["pids"][4321] = GAME
    element = _Element()
    with pytest.raises(PermissionError):
        _LiveSlider(element, _Pattern()).focus()
    assert element.focus_calls == 0
    game["pids"].clear()
    _LiveSlider(element, _Pattern()).focus()
    assert element.focus_calls == 1


# -- the source walk: no unguarded call ------------------------------------------------------------------------------

#: Calls that send input, take focus, show or activate a window, open a link or launch a program.
SENSITIVE = {"SendInput", "SetCursorPos", "keybd_event", "mouse_event", "SetForegroundWindow", "BringWindowToTop",
             "SetActiveWindow", "SwitchToThisWindow", "AttachThreadInput", "SetFocus", "ShowWindow", "ShowWindowAsync",
             "startfile", "ShellExecute", "ShellExecuteW", "ShellExecuteEx", "ShellExecuteExW", "ActivateApplication",
             "ActivateForProtocol", "CreateProcess", "CreateProcessW", "WinExec"}
#: ``subprocess`` / ``os`` launchers (by receiver).
LAUNCHERS = {("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "check_call"),
             ("subprocess", "check_output"), ("os", "system"), ("os", "spawnv"), ("os", "spawnl")}
#: Reviewed exceptions, each with its reason.
ALLOWED = {
    # the PowerShell host Yuki runs its scripts in: a hidden console process, never a window; the GUI launches it
    # carries (Start-Process) are checked at their own run_powershell call sites below
    ("actions/shell.py", "Popen"),
}
#: Read-only PowerShell (it lists the Start menu; it starts nothing).
READ_ONLY_POWERSHELL = {("actions/launch.py", "_powershell_json")}
SCOPES = ("actions", "perception", "skills", "connect")


def _is_guard(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "refuse_input")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "refuse_input"))


_GUARDS: dict[int, list[int]] = {}


def _guards_in(fn: ast.AST) -> list[int]:
    """Lines of the guard calls a function tests in an ``if`` (its own body, not nested functions')."""
    if id(fn) in _GUARDS:
        return _GUARDS[id(fn)]
    lines: list[int] = _GUARDS.setdefault(id(fn), [])

    def walk(node: ast.AST, top: bool) -> None:
        if not top and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(node, (ast.If, ast.While)) and any(_is_guard(n) for n in ast.walk(node.test)):
            lines.append(node.test.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, False)

    walk(fn, True)
    return lines


def _sensitive_calls() -> list[tuple[str, str, int, list[ast.AST], str]]:
    """(file, call name, line, enclosing functions innermost first, source) of every sensitive call in scope."""
    _GUARDS.clear()        # keyed by node id: only valid while these trees are alive
    out = []
    for scope in SCOPES:
        base = ROOT / scope
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            stack: list[ast.AST] = []

            def walk(node: ast.AST) -> None:
                named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if named:
                    stack.append(node)
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) \
                        else ""
                    receiver = (func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                                else "")
                    text = (ast.get_source_segment(src, node) or "") if name == "run_powershell" else name
                    hit = (name in SENSITIVE or (receiver, name) in LAUNCHERS
                           or (name == "run_powershell" and ("Start-Process" in text or rel == "actions/launch.py")))
                    if hit and not (name == "run_powershell" and stack
                                    and (rel, stack[-1].name) in READ_ONLY_POWERSHELL):
                        out.append((rel, name, node.lineno, list(reversed(stack)), text))
                for child in ast.iter_child_nodes(node):
                    walk(child)
                if named:
                    stack.pop()

            walk(tree)
    return out


def test_no_input_focus_link_or_launch_call_is_unguarded() -> None:
    calls = _sensitive_calls()
    assert len(calls) >= 15                                     # the walk sees the known call sites
    unguarded = []
    for rel, name, line, enclosing, text in calls:
        if (rel, name) in ALLOWED:
            continue
        if not any(g < line for fn in enclosing for g in _guards_in(fn)):
            where = enclosing[0].name if enclosing else "<module>"
            unguarded.append(f"{rel}:{line} {where}: {text[:80]}")
    assert unguarded == [], "unguarded (call refuse_input in an if before it):\n" + "\n".join(unguarded)


def test_the_walk_catches_an_unguarded_call(tmp_path: Path, monkeypatch) -> None:
    """The walk itself: a planted unguarded SetFocus in a scope folder is reported, a guarded one is not."""
    planted = tmp_path / "yuki"
    (planted / "connect").mkdir(parents=True)
    (planted / "connect" / "bad.py").write_text(
        "def warm(element):\n    element.SetFocus()\n\n"
        "def good(element, gamegate):\n    if gamegate.refuse_input('uia_focus') is not None:\n        return\n"
        "    element.SetFocus()\n", encoding="utf-8")
    globals()["ROOT"] = planted
    try:
        calls = _sensitive_calls()
        flagged = [c for c in calls if not any(g < c[2] for fn in c[3] for g in _guards_in(fn))]
        assert [(c[0], c[3][0].name) for c in flagged] == [("connect/bad.py", "warm")]
    finally:
        globals()["ROOT"] = Path(__file__).resolve().parents[1] / "yuki"
