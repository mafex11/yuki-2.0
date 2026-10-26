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

#: These tests stub the game facts themselves: a test-run stub of the live game check (review r2 L8)
#: stays off for them.
LIVE_GAMEGATE = True

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


@pytest.fixture
def quiet_reads(monkeypatch):
    """The public actions' reads around the send (hit test, page, focus, windows) as no-ops: only the send and the
    game checks are real here."""
    import yuki.actions.input as inp

    stubs = {"_start_hit_test": None, "_finish_hit_test": (None, "stub"), "_start_page_read": None,
             "_finish_page_read": (False, None, {}), "_focus_state": {}, "_start_page_watch": None,
             "_report_effects": "nothing changed", "_refuse_unless_ready": None, "_windows_before": (0, {}),
             "_page_before_keys": (0, None, False), "_window_state_before": (0, None), "_window_state_after": "",
             "_windows_appeared": [], "_describe_appeared": "", "_input_target": ("the field", {}),
             "_where_focus_is": ("the field", {})}
    for name, answer in stubs.items():
        monkeypatch.setattr(inp, name, lambda *a, _answer=answer, **k: _answer)
    return inp


def _front_after(monkeypatch, checks: int) -> None:
    """The game comes to the front after ``checks`` game checks."""
    calls = {"n": 0}

    def front() -> str:
        calls["n"] += 1
        return GAME if calls["n"] > checks else ""

    monkeypatch.setattr(G, "game_in_front", front)


def test_a_game_coming_to_the_front_mid_action_is_never_reported_done(game, user32, quiet_reads, monkeypatch) -> None:
    """Review r2 L2: click, press, hotkey and Enter check what the primitives did; a refusal after the up-front check
    is a failed result with ``refused = "game"``, never "done"; a click never leaves a button held down."""
    inp = quiet_reads
    game["front"] = ""
    _front_after(monkeypatch, 1)                       # up-front check passes; the pointer move is refused
    result = inp.click(10, 10)
    assert not result.ok and result.details["refused"] == "game" and "not moved" in result.summary
    assert user32.names().count("SetCursorPos") == 0 and user32.names().count("SendInput") == 0
    _front_after(monkeypatch, 3)                       # the first press+release goes out, the second is refused
    result = inp.click(10, 10, clicks=2)
    assert not result.ok and result.details["refused"] == "game" and "after 1 of 2 clicks" in result.summary
    assert user32.names().count("SendInput") == 2      # down and up of the first click: no lone button-down
    for action in (lambda: inp.press("enter"), lambda: inp.hotkey("ctrl", "t")):
        user32.calls.clear()
        _front_after(monkeypatch, 1)
        result = action()
        assert not result.ok and result.details["refused"] == "game" and "was not pressed" in result.summary
        assert "SendInput" not in user32.names()
    _front_after(monkeypatch, 1)
    result = inp.type_text("", press_enter=True)
    assert not result.ok and result.details["refused"] == "game" and "Enter was not pressed" in result.summary
    # the clear before typing: a field without a readable value used to say "sent select-all + delete"
    field = type("Focus", (), {"ok": True, "accepts_text": True, "value": "old", "has_value": False,
                               "describe": lambda self: "Edit"})()
    monkeypatch.setattr(inp, "_read_focus", lambda *a, **k: field)
    user32.calls.clear()
    _front_after(monkeypatch, 0)
    details: dict = {}
    failure, phrase = inp._clear_focused(None, details)
    assert failure and "did not clear" in failure and details["refused"] == "game" and details["cleared"] is False
    assert "SendInput" not in user32.names()


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

#: Calls that send input, take focus, show, raise or activate a window, open a link or launch a program (Win32 names,
#: and uiautomation's own input and window methods).
SENSITIVE = {"SendInput", "SetCursorPos", "keybd_event", "mouse_event", "SetForegroundWindow", "BringWindowToTop",
             "SetActiveWindow", "SwitchToThisWindow", "AttachThreadInput", "SetFocus", "ShowWindow", "ShowWindowAsync",
             "SetWindowPos", "SetWindowPlacement", "OpenIcon", "startfile", "ShellExecute", "ShellExecuteW",
             "ShellExecuteEx", "ShellExecuteExW", "ActivateApplication", "ActivateForProtocol", "CreateProcess",
             "CreateProcessW", "WinExec", "SendKeys", "SendKey", "PressKey", "Click", "DoubleClick", "RightClick",
             "MiddleClick", "DragDrop", "MoveCursorToMyCenter", "MoveCursorToInnerPos", "WheelDown", "WheelUp",
             "SetTopmost", "SetActive", "SetWindowVisualState"}
#: Window messages posted or sent to a window: sensitive (WM_KEYDOWN, WM_CHAR, WM_CLOSE, WM_SYSCOMMAND, a mouse
#: message...) unless the message is one of the read-only ones, by its name or its number; a message held in a variable
#: counts as sensitive.
MESSAGE_CALLS = {"PostMessage", "PostMessageW", "PostMessageA", "SendMessage", "SendMessageW", "SendMessageA",
                 "SendMessageTimeout", "SendMessageTimeoutW", "SendMessageTimeoutA", "SendNotifyMessage",
                 "SendNotifyMessageW", "PostThreadMessage", "PostThreadMessageW"}
READ_ONLY_MESSAGES = {"WM_NULL": 0x0000, "WM_GETTEXT": 0x000D, "WM_GETTEXTLENGTH": 0x000E, "WM_GETOBJECT": 0x003D}
#: Launchers by module (``subprocess.Popen``, ``sp.Popen`` after ``import subprocess as sp``, ``Popen`` after
#: ``from subprocess import Popen``).
LAUNCHERS = ({("subprocess", n) for n in ("Popen", "run", "call", "check_call", "check_output", "getoutput",
                                          "getstatusoutput")}
             | {("os", n) for n in ("system", "popen", "startfile", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                                    "spawnl", "spawnle", "spawnlp", "spawnlpe", "execv", "execve", "execvp",
                                    "execvpe", "execl", "execle", "execlp", "execlpe")}
             | {("webbrowser", n) for n in ("open", "open_new", "open_new_tab")}
             | {("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell")})
#: Reviewed exceptions, each with its reason.
ALLOWED = {
    # the PowerShell host Yuki runs its scripts in: a hidden console process, never a window; the GUI launches it
    # carries are checked at their own run_powershell call sites (every one in scope is walked)
    ("actions/shell.py", "Popen"),
    # WM_QUIT to Yuki's own raw-input listener thread (the thread id it started), ending its message loop: no window
    ("actions/userinput.py", "PostThreadMessageW"),
    # r30 (read 27 Oct): pSee's placement helpers `_show` / `_set_rect` (one ShowWindow, two SetWindowPos) are only
    # reached after their callers asked the hands' gate (`refused = _gate.refuse("window_place" | "window_maximize",
    # hwnd=...)`, then leave on a refusal), an assignment the walk does not read as a guard
    ("actions/place.py", "ShowWindow"),
    ("actions/place.py", "SetWindowPos"),
    # mb (21 Oct): the display route's `_run_app` launch (subprocess.run) is only reached after its callers check
    # `_launch_held` / `gate.refuse("launch")`
    ("skills/display_route.py", "run"),
    # chat19: `apply_pattern`'s "focus" (SetFocus) is reached only from `press()`, which asks the gate first
    ("skills/uia_route.py", "SetFocus"),
}
#: Read-only PowerShell (it lists the Start menu; it starts nothing). Every other ``run_powershell`` call in scope is
#: walked, whatever its script (a script in a variable may start anything).
READ_ONLY_POWERSHELL = {("actions/launch.py", "_powershell_json")}
SCOPES = ("actions", "perception", "skills", "connect")


#: The guard calls: the game gate's own ``refuse_input``, and the hands' gate ``yuki.actions.gate.refuse`` (fHands,
#: 18 Oct: the hands ask it, and it asks ``refuse_input`` among its checks).
GUARDS = {"refuse_input", "refuse"}


def _is_guard(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id in GUARDS)
        or (isinstance(node.func, ast.Attribute) and node.func.attr in GUARDS))


def _is_guard_stmt(node: ast.AST) -> bool:
    """``if <refuse_input(...) ...>:`` whose body leaves (returns, raises, or skips the rest of its loop body)."""
    return (isinstance(node, ast.If) and any(_is_guard(n) for n in ast.walk(node.test))
            and any(isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)) for st in node.body))


def _guarded(path: list[ast.AST]) -> bool:
    """Whether a call is always preceded by a guard: a guard statement earlier in a block that holds the call (the
    call's own block or one around it), inside the call's function or a function enclosing it. A guard in another
    branch, after the call, or one whose body goes on to the call anyway does not count."""
    first_fn = next((i for i, n in enumerate(path) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if first_fn is None:
        return False
    for parent, child in zip(path[first_fn:], path[first_fn + 1:]):
        for _field, value in ast.iter_fields(parent):
            if isinstance(value, list) and any(item is child for item in value):
                for item in value:
                    if item is child:
                        break
                    if _is_guard_stmt(item):
                        return True
    return False


def _message_is_read_only(node: ast.Call) -> bool:
    if len(node.args) < 2:
        return False
    msg = node.args[1]
    if isinstance(msg, ast.Constant) and isinstance(msg.value, int):
        return msg.value in READ_ONLY_MESSAGES.values()
    name = msg.id if isinstance(msg, ast.Name) else msg.attr if isinstance(msg, ast.Attribute) else ""
    return name.lstrip("_") in READ_ONLY_MESSAGES


def _sensitive_calls() -> list[tuple[str, str, int, list[ast.AST], str]]:
    """(file, call name, line, path from the module down to the call, source) of every sensitive call in scope."""
    out = []
    for scope in SCOPES:
        base = ROOT / scope
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            modules: dict[str, str] = {}               # local name -> module (import x / import x as y)
            names: dict[str, tuple[str, str]] = {}     # local name -> (module, name) (from x import y [as z])
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        modules[alias.asname or alias.name.split(".")[0]] = alias.name
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        names[alias.asname or alias.name] = (node.module, alias.name)
            stack: list[ast.AST] = []

            def walk(node: ast.AST) -> None:
                stack.append(node)
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        receiver = func.value.id if isinstance(func.value, ast.Name) else ""
                        launcher = (modules.get(receiver, receiver), func.attr)
                    elif isinstance(func, ast.Name):
                        launcher = names.get(func.id, ("", func.id))
                    else:
                        launcher = ("", "")
                    real = launcher[1]
                    fns = [n for n in stack if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    hit = (real in SENSITIVE or launcher in LAUNCHERS
                           or (real in MESSAGE_CALLS and not _message_is_read_only(node))
                           or (real == "run_powershell"
                               and not (fns and (rel, fns[-1].name) in READ_ONLY_POWERSHELL)))
                    if hit:
                        text = (ast.get_source_segment(src, node) or "") if real == "run_powershell" else real
                        out.append((rel, real, node.lineno, list(stack), text))
                for child in ast.iter_child_nodes(node):
                    walk(child)
                stack.pop()

            walk(tree)
    return out


def _where(path: list[ast.AST]) -> str:
    fns = [n for n in path if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return fns[-1].name if fns else "<module>"


def test_no_input_focus_link_or_launch_call_is_unguarded() -> None:
    calls = _sensitive_calls()
    assert len(calls) >= 25                                     # the walk sees the known call sites
    unguarded = [f"{rel}:{line} {_where(path)}: {text[:80]}" for rel, name, line, path, text in calls
                 if (rel, name) not in ALLOWED and not _guarded(path)]
    assert unguarded == [], ("unguarded (put `if refuse_input(...) is not None: return` before it, in the same "
                             "block or one around it):\n" + "\n".join(unguarded))


_PLANTED = '''import subprocess as sp
import webbrowser
from subprocess import Popen
from os import system as run_it
import win32gui, win32con


def warm(element):
    element.SetFocus()


def good(element, gamegate):
    if gamegate.refuse_input("uia_focus") is not None:
        return
    element.SetFocus()


def other_branch(element, gamegate, x):
    if x:
        if gamegate.refuse_input("uia_focus") is not None:
            return
    else:
        element.SetFocus()


def no_exit(element, gamegate, log):
    if gamegate.refuse_input("uia_focus") is not None:
        log("refused")
    element.SetFocus()


def after_call(element, gamegate):
    element.SetFocus()
    if gamegate.refuse_input("uia_focus") is not None:
        return


def launches(url, cmd):
    sp.Popen(cmd)
    Popen(cmd)
    run_it(cmd)
    webbrowser.open(url)


def messages(hwnd, msg):
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    win32gui.SendMessage(hwnd, 0x0102, 65, 0)
    win32gui.SendMessage(hwnd, msg, 0, 0)
    win32gui.SendMessageTimeoutW(hwnd, _WM_GETOBJECT, 0, 0, 2, 100, None)
    win32gui.SendMessage(hwnd, 0x000E, 0, 0)


def raises(hwnd, element):
    win32gui.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 3)
    element.SendKeys("x")
    element.Click()


def in_loop(hwnds, gamegate, element):
    for hwnd in hwnds:
        if gamegate.refuse_input("focus", hwnd=hwnd) is not None:
            continue
        element.SetFocus()
'''


def test_the_walk_catches_unguarded_calls_of_every_shape(tmp_path: Path) -> None:
    """The walk itself (review r2 L7), on planted files: every shape below is flagged unless a guard that leaves comes
    first in a block holding the call; read-only messages and guarded calls are not."""
    planted = tmp_path / "yuki"
    (planted / "connect").mkdir(parents=True)
    (planted / "skills").mkdir(parents=True)
    (planted / "connect" / "bad.py").write_text(_PLANTED, encoding="utf-8")
    (planted / "skills" / "ps.py").write_text(
        "from yuki.actions.shell import run_powershell\n\n\ndef any_script(script):\n    return run_powershell(script)\n",
        encoding="utf-8")
    globals()["ROOT"] = planted
    try:
        calls = _sensitive_calls()
        flagged = sorted((c[0], _where(c[3]), c[1]) for c in calls if not _guarded(c[3]))
        bad = "connect/bad.py"
        assert flagged == sorted([
            (bad, "warm", "SetFocus"), (bad, "other_branch", "SetFocus"), (bad, "no_exit", "SetFocus"),
            (bad, "after_call", "SetFocus"),
            (bad, "launches", "Popen"), (bad, "launches", "Popen"), (bad, "launches", "system"),
            (bad, "launches", "open"),
            (bad, "messages", "PostMessage"), (bad, "messages", "SendMessage"), (bad, "messages", "SendMessage"),
            (bad, "raises", "SetWindowPos"), (bad, "raises", "SendKeys"), (bad, "raises", "Click"),
            ("skills/ps.py", "any_script", "run_powershell"),
        ])
        assert sorted((_where(c[3]), c[1]) for c in calls if _guarded(c[3])) == [("good", "SetFocus"),
                                                                                 ("in_loop", "SetFocus")]
    finally:
        globals()["ROOT"] = Path(__file__).resolve().parents[1] / "yuki"
